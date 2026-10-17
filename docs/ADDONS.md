# Writing crawler add-ons

An add-on finds files somewhere and hands them to Median. Median does everything else: duplicate checks, thumbnails, review, tags. The built-in demo add-on (`app/addons/demo.py`) is the reference implementation; read it first.

## Registering an add-on

Two ways, both discovered when web and worker start (restart both after adding or changing an add-on):

1. An entry point in the group `media_archive.addons` of an installed package. Built-in add-ons register this way in `pyproject.toml`:

   ```toml
   [project.entry-points."media_archive.addons"]
   demo = "app.addons.demo:DemoAddon"
   ```

2. A single file `DATA_DIR/addons/<name>.py` that defines a module-level `ADDON` (an `Addon` subclass or an instance). Files starting with `_` or `.` are ignored.

An add-on that fails to import, has no `ADDON`, has an invalid key or a synchronous `run`, or reuses a key that is already taken is listed under "Add-on errors" on /sources (and in the logs). It never stops the app. When two add-ons have the same key, the first one wins: entry points (in name order) before files (in file name order).

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
    key = "example"  # 1-50 of a-z, 0-9, _ ; stored as file_sources.source_key
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

### Config forms

/sources builds the create and edit form from `ConfigModel`'s JSON schema. Supported field types: `str`, `int`, `float`, `bool`, enums and `Literal[...]` (select), `list[str]` (one value per line), `SecretFile` (file upload). Optional fields (`X | None`) work. Anything else is edited as JSON. Field titles and descriptions come from Pydantic (`Field(description=...)`).

`SecretFile` fields are never stored in the database. The uploaded file is kept as `DATA_DIR/secrets/<source_id>/<field>` with mode 0600 and the add-on receives its path (or `None`).

### CrawlContext

| Member | Meaning |
| --- | --- |
| `work_dir` | Empty directory for this run. Removed when the run ends, whatever the outcome. Emitted files are already moved out. |
| `state_dir` | Persistent directory per source (`DATA_DIR/addon_state/<source_id>/`), for example a native download archive. Also `HOME` of subprocesses. |
| `secrets_dir` | `DATA_DIR/secrets/<source_id>/`. |
| `log` | A `logging.Logger` writing to `logs/jobs/<run_id>.log` (shown live on /jobs/<id>) and stdout. |
| `await emit(item)` | Hands a file inside `work_dir` to the ingest service. Tags are crawler tags; the source's default tags are added. |
| `await is_known(remote_id)`, `await known_remote_ids()` | Remote ids this add-on key already delivered, files in any status (discarded included). |
| `is_cancelled()` | True within 2 seconds of a cancel request (or when the runtime limit is hit). Check it between items. |
| `await sleep(seconds)` | Sleeps, returns True early if the run was cancelled. |
| `await run_subprocess(args, timeout, cwd=None, env=None)` | Runs a program from an argument list (never a shell) in its own process group. The environment is only `PATH`, `LANG`, `HOME` (= `state_dir`) and `env`. Output goes line by line to the job log. The whole group is killed on timeout or cancellation. Returns `SubprocessResult(returncode, tail, timed_out, cancelled)` with the last 50 lines. |

### Runs

- A run is queued by Run now (`POST /api/sources/<id>/run`) or by the cron schedule (checked every minute, read in `APP_TIMEZONE`). A source never has two queued or running runs.
- On cancel, `is_cancelled()` turns true and subprocesses are stopped; an add-on that does not return within 5 seconds is cancelled (`asyncio.CancelledError`). The run is marked cancelled.
- After `max_runtime_minutes` (per source, default 120) the run is stopped the same way and marked failed.
- An exception from `run()` marks the run failed with the message; the traceback is in the job log.
- Emitting the same `remote_id` again is safe: ingest skips known remote ids before hashing (`skipped_known_remote`). Using `known_remote_ids()` first only saves the download.
- Run stats: `emitted` is counted by the context; new, duplicate_exact, skipped_known_remote and errors come from the ingest events of the run.
