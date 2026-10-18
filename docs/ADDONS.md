# Writing crawler add-ons

An add-on finds files somewhere and hands them to Median. Median does everything else: duplicate checks, thumbnails, review, tags. The built-in demo add-on (`app/addons/demo.py`) is the smallest reference implementation; `app/addons/ytdlp.py` and `app/addons/gallerydl.py` show how to drive an external program, `app/addons/direct.py` how to download over HTTP.

## Built-in add-ons

| Key | What it does | Config |
| --- | --- | --- |
| `demo` | Generates test images (no network). | count, seed, tags, delay_seconds |
| `ytdlp` | Video sites through yt-dlp. remote_id is yt-dlp's download-archive line (`<extractor key in lowercase> <video id>`, for example `youtube dQw4w9WgXcQ`). Tags: the video's `tags`, its `categories` (optional) and `uploader:<name>` (optional). | urls, format (`bv*+ba/b`), max_items, date_after (YYYYMMDD), include_uploader_tag, include_categories, cookies, extra_args |
| `gallerydl` | Image sites through gallery-dl. remote_id `<category>:<id>:<num>` (`<category>:<id>` without num), or `<category>:<filename>` when the item has no id. Tags from `tag_fields`. | urls, tag_fields (`["tags", "artist=artist"]`), range, cookies, extra_args |
| `direct` | Plain file URLs over HTTP(S). remote_id is the URL with lowercase scheme and host and no fragment. Filename from Content-Disposition, else the URL path. | urls, tags, max_size_mb (2048) |

yt-dlp and gallery-dl live in their own venv, `DATA_DIR/tools/venv`, never in the app's environment. The worker installs or upgrades them to the latest versions every time it starts (`python -m app.worker`), and `python -m app.cli tools` does it on demand; the installed versions are in `DATA_DIR/tools/versions.json`. The image contains Deno, the JavaScript runtime yt-dlp needs for YouTube (`yt-dlp --verbose` prints `[debug] JS runtimes: deno-...`). `app.crawl.tools.tool_path(name)` returns a tool's executable; `ytdlp` and `gallerydl` take an `executable` config override, meant for tests.

`tag_fields` entries are `field` or `field=namespace`. A field may hold a list (each item is one tag, spaces kept) or a string (split on spaces); nested fields use dots (`author.name`). With a namespace, each value becomes `namespace:value`.

`extra_args` (one option per line) are appended to the tool's command line after Median's own options. Options that run commands, change where files go, load other configuration, code or URL lists, or evaluate Python are refused when the source is saved, including abbreviations (`--exec-b`), the `--opt=value` form and short option clusters (`-ixo out`); see `app/crawl/safety.py` for the lists. A value that starts with `-` must be written as `--option=value`. Both tools also run with their configuration files ignored (`--ignore-config`, `--config-ignore`).

yt-dlp: a run fails when yt-dlp exits with an error and emitted nothing; when it fails after emitting files, the run succeeds with a warning in the log (exit 101, the `max_items` limit, is a success). Before each run `state_dir/ytdlp-archive.txt` is rewritten with every remote id the database knows for `ytdlp`, so a lost state directory never causes re-downloads. gallery-dl keeps its own archive at `state_dir/gallery-dl-archive.sqlite3`; if it is lost, the known-remote check in ingest still drops the files before hashing.

## Registering an add-on

Two ways, both discovered when web and worker start (restart both after adding or changing an add-on):

1. An entry point in the group `media_archive.addons` of an installed package. Built-in add-ons register this way in `pyproject.toml`:

   ```toml
   [project.entry-points."media_archive.addons"]
   demo = "app.addons.demo:DemoAddon"
   ```

2. A single file `DATA_DIR/addons/<name>.py` that defines a module-level `ADDON` (an `Addon` subclass or an instance). Files starting with `_` or `.` are ignored.

An add-on that fails to import, has no `ADDON`, has an invalid or reserved key (`upload`), a `ConfigModel` that is not a Pydantic model or has no JSON schema, or a synchronous or missing `run`, or reuses a key that is already taken is listed under "Add-on errors" on /sources (and in the logs). It never stops the app. When two add-ons have the same key, the first one wins: entry points (in name order) before files (in file name order).

## The interface

```python
from pathlib import Path

from pydantic import BaseModel, Field

from app.crawl.base import Addon, CrawlContext, CrawledItem, SecretFile


class Config(BaseModel):
    url: str
    max_items: int = Field(default=50, ge=1)
    cookies: SecretFile = None  # uploaded on the source form, passed as a Path or None


class Example(Addon):
    key = "example"  # 1-50 of a-z, 0-9, _ ("upload" is reserved); file_sources.source_key
    name = "Example site"
    version = "1.0"
    description = "Downloads the newest posts of a feed."
    ConfigModel = Config

    async def run(self, ctx: CrawlContext, config: Config) -> None:
        known = await ctx.known_remote_ids()
        for post in fetch_posts(config.url, config.max_items):
            if ctx.is_cancelled():
                break
            if post.id in known:
                continue
            path = ctx.work_dir / post.filename
            path.write_bytes(post.data)
            await ctx.emit(
                CrawledItem(
                    path=path,
                    remote_id=post.id,
                    url=post.url,
                    title=post.title,
                    tags=post.tags,
                    metadata={"author": post.author},
                )
            )


ADDON = Example  # only needed for single-file add-ons
```

`Addon` has the class attributes `key`, `name`, `version` (default `"0"`), `description` and `ConfigModel` (default: an empty model), and `async run(ctx, config)`, which receives a validated `ConfigModel` instance. Registration instantiates a subclass without arguments.

### CrawledItem

| Field | Meaning |
| --- | --- |
| `path` | The file, inside `work_dir` (a relative path is taken relative to it). It is moved into staging. |
| `remote_id` | Stable id of the item for this add-on key, or `None` (see below). |
| `url` | Page a person would open for this item. |
| `title`, `description` | Stored on the file when it is new. |
| `tags` | Crawler tags (the source's default tags are added). |
| `metadata` | JSON-serializable dict, stored as `file_sources.raw_metadata`. |
| `filename` | Original file name shown in Median; defaults to `path.name`. |

### Config forms

/sources builds the create and edit form from `ConfigModel`'s JSON schema. Supported field types: `str`, `int`, `float`, `bool`, enums and `Literal[...]` (select), `list[str]` (one value per line), `SecretFile` (file upload). Optional fields (`X | None`) work. Anything else is edited as JSON. Field titles and descriptions come from Pydantic (`Field(description=...)`).

`SecretFile` fields are never stored in the database. The uploaded file is kept as `DATA_DIR/secrets/<source_id>/<field>` with mode 0600 and the add-on receives its path (or `None`).

### CrawlContext

| Member | Meaning |
| --- | --- |
| `work_dir` | Empty directory for this run. Removed when the run ends, whatever the outcome. Emitted files are already moved out. |
| `state_dir` | Persistent directory per source (`DATA_DIR/addon_state/<source_id>/`), for example a native download archive. Also `HOME` of subprocesses. |
| `secrets_dir` | `DATA_DIR/secrets/<source_id>/`. |
| `source_id`, `job_run_id` | Ids of the source and of this run. |
| `emitted` | Number of items emitted so far in this run. |
| `log` | A `logging.Logger` writing to `logs/jobs/<run_id>.log` (shown live on /jobs/<id>) and stdout. |
| `await emit(item)` | Hands a file inside `work_dir` to the ingest service. Tags are crawler tags; the source's default tags are added. |
| `await is_known(remote_id)`, `await known_remote_ids()` | Remote ids this add-on key already delivered, files in any status (discarded included). |
| `is_cancelled()` | True within 2 seconds of a cancel request (or when the runtime limit is hit). Check it between items. |
| `await sleep(seconds)` | Sleeps, returns True early if the run was cancelled. |
| `await run_subprocess(args, timeout=3600, cwd=None, env=None)` | Runs a program from an argument list (never a shell; a string is a `TypeError`) in its own process group, in `cwd` (default `work_dir`). The environment is only `PATH`, `LANG`, `HOME` (= `state_dir`), the proxy and CA bundle variables when the worker has them, and `env`. Output goes line by line to the job log. The whole group is killed on timeout or cancellation. Returns `SubprocessResult(returncode, tail, timed_out, cancelled)` with the last 50 lines; `result.ok` is true for exit code 0 without timeout or cancellation. |

### Runs

- A run is queued by Run now (`POST /api/sources/<id>/run`) or by the cron schedule (checked every minute, read in `APP_TIMEZONE`). A source never has two queued or running runs.
- On cancel, `is_cancelled()` turns true and subprocesses are stopped; an add-on that does not return within 5 seconds is cancelled (`asyncio.CancelledError`). The run is marked cancelled.
- After `max_runtime_minutes` (per source, default 120) the run is stopped the same way and marked failed.
- An exception from `run()` marks the run failed with the message; the traceback is in the job log.
- Emitting the same `remote_id` again is safe: ingest skips known remote ids before hashing (`skipped_known_remote`). Using `known_remote_ids()` first only saves the download.
- Run stats: `emitted` is counted by the context; new, duplicate_exact, skipped_known_remote and errors come from the ingest events of the run.

## Rules for add-on authors

### remote_id

- Stable: the same item gets the same id on every run, whatever its URL parameters, title or download order. Prefer the site's own id (`<site>:<post id>:<file number>`), never a temporary download URL.
- Unique within the add-on key: ids are compared per add-on key across all its sources, so two sources of one add-on share their known ids (a file found by both is downloaded once).
- Ingest skips an item whose `(add-on key, remote_id)` is already recorded, in any status, discarded included, before hashing it (`skipped_known_remote`). Check `await ctx.is_known(id)` or `await ctx.known_remote_ids()` before downloading to save the download.
- `None` is allowed for things without identity. Such files are only deduplicated by content (SHA-256).

### Tags

Pass the site's tags as they are; Median normalizes them (NFKC, lowercase, spaces to `_`, at most 100 characters) and drops empty ones. Use `namespace:value` for structured tags (`artist:ann`, `uploader:jane_doe`); the namespace must be `[a-z0-9_]+`. Tags from add-ons are crawler tags: the user can remove them, and a removed tag is never added back by a later crawl or duplicate. The source's default tags are added to every item.

### Metadata

`CrawledItem.metadata` is stored as `file_sources.raw_metadata` (JSON) for this source of the file. Keep it JSON-serializable and reasonably small: ids, uploader, dates, durations, the site's own fields. Title and description have their own fields; `url` should be the page a person would open, not the media URL.

### Running external programs

- Always `await ctx.run_subprocess([...])` with an argument list, never a shell and never a string. User input goes in as separate arguments, after `--` when the program supports it, so a value can never become an option.
- If users can add options, validate them against a denylist (`app.crawl.safety.OptionPolicy`) in a Pydantic validator, so bad options are refused when the source is saved, not when it runs.
- Only `PATH`, `LANG`, `HOME` (= `state_dir`), the proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, lowercase forms) and `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` reach the program; pass anything else with `env=`. Point every output path into `ctx.work_dir` and persistent state into `ctx.state_dir`. Keep credentials in `SecretFile` fields.
- Emit files while the program runs (see `app.crawl.watch`: `run_while_polling`, `LineTail` for a progress file, `StableFiles` for files with sidecars whose size must settle), so a long crawl shows progress and a cancelled one keeps what it already got.
- Install programs into `DATA_DIR/tools/venv` (Python tools) or the image, never into the app's environment.

### Testing with fake executables

Tests never use the network. Replace the real program with a small script that behaves like it: it parses the options your add-on passes, writes the files, sidecars and progress lines the real tool writes, and has modes for slow downloads and failures. `tests/crawl/fakes/fake_ytdlp.py` and `fake_gallerydl.py` are examples; `tests.crawl.helpers.fake_tool()` copies one into an executable with the test's Python, and the add-on's `executable` config field points to it. A slow mode that waits until the add-on has taken (moved) a file before writing the next one proves that files are emitted while the program runs. Real-network checks belong in tests marked `network` (skipped by default), reading their URLs from environment variables (`tests/crawl/test_network.py`).

## A complete single-file add-on

Save as `DATA_DIR/addons/curl_list.py` and restart web and worker. It downloads a list of URLs with `curl`, showing the pattern for any command-line downloader. `curl` is not in the Median image: install it in a derived image, or use the built-in `direct` add-on, which does the same with httpx.

```python
"""Downloads plain URLs with curl, one subprocess per URL."""

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.crawl.base import Addon, CrawlContext, CrawledItem
from app.storage.paths import safe_filename


class CurlListConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urls: list[str] = Field(description="http(s) URLs, one per line")
    tags: list[str] = Field(default_factory=list, description="Tags for every file")
    max_seconds: int = Field(default=600, ge=10, le=86_400, description="Per download")

    @field_validator("urls")
    @classmethod
    def _http_only(cls, urls: list[str]) -> list[str]:
        for url in urls:
            if urlsplit(url).scheme not in ("http", "https"):
                raise ValueError(f"{url!r} is not an http(s) URL")
        return urls


class CurlListAddon(Addon):
    key = "curl_list"
    name = "URL list (curl)"
    version = "1.0"
    description = "Downloads each URL of a list with curl."
    ConfigModel = CurlListConfig

    async def run(self, ctx: CrawlContext, config: CurlListConfig) -> None:
        known = await ctx.known_remote_ids()
        for n, url in enumerate(config.urls, 1):
            if ctx.is_cancelled():
                break
            if url in known:
                ctx.log.info("already have %s", url)
                continue
            name = safe_filename(urlsplit(url).path.rsplit("/", 1)[-1] or "download")
            target = ctx.work_dir / f"{n}-{name}"
            result = await ctx.run_subprocess(
                ["curl", "--fail", "--location", "--silent", "--show-error",
                 "--output", str(target), "--", url],
                timeout=config.max_seconds,
            )  # fmt: skip
            if not result.ok or not target.is_file():
                ctx.log.error("curl failed for %s: %s", url, result.tail[-1:])
                continue
            await ctx.emit(
                CrawledItem(path=target, remote_id=url, url=url, filename=name, tags=config.tags)
            )
        ctx.log.info("emitted %d file(s)", ctx.emitted)


ADDON = CurlListAddon
```

The same add-on as a package: put the module in a package, register it in that package's `pyproject.toml` under `[project.entry-points."media_archive.addons"]` (`curl_list = "my_addons.curl_list:CurlListAddon"`), and install the package into the app's environment (for example with a derived image). Drop the `ADDON` line.
