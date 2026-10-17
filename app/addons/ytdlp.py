"""The yt-dlp add-on: downloads videos (and audio) from the sites yt-dlp supports.

yt-dlp runs as a subprocess from the tool venv. Every finished file is announced by a line that
--print-to-file appends to work_dir/done.tsv at the after_move stage; the add-on polls that file
and emits each file with its .info.json while yt-dlp is still running.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.crawl import watch
from app.crawl.base import Addon, CrawlContext, CrawledItem, SecretFile
from app.crawl.safety import check_urls, check_ytdlp_args
from app.crawl.tools import tool_path
from app.storage.fs import atomic_write_bytes

ARCHIVE_NAME = "ytdlp-archive.txt"
DONE_NAME = "done.tsv"
POLL_SECONDS = 2.0
SUBPROCESS_TIMEOUT = 7 * 24 * 3600.0  # the source's max_runtime_minutes is the real limit
EXIT_MAX_DOWNLOADS = 101  # yt-dlp stopped because --max-downloads was reached
# One line per finished file: "<extractor key> <id>", final path, info JSON path.
DONE_TEMPLATE = "%(extractor_key)s %(id)s\t%(filepath)s\t%(infojson_filename)s"
METADATA_KEYS = ("id", "uploader", "upload_date", "duration", "extractor")


class YtdlpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urls: list[str] = Field(description="Video, playlist or channel URLs, one per line")
    format: str = Field(default="bv*+ba/b", description="yt-dlp format selection (-f)")
    max_items: int | None = Field(
        default=None, ge=1, description="Stop after this many downloads (--max-downloads)"
    )
    date_after: str | None = Field(
        default=None,
        pattern=r"^\d{8}$",
        description="Only videos uploaded on or after this date, YYYYMMDD (--dateafter)",
    )
    include_uploader_tag: bool = Field(default=True, description='Add the tag "uploader:<name>"')
    include_categories: bool = Field(default=True, description="Add the video's categories")
    cookies: SecretFile = Field(default=None, description="Netscape cookies.txt")
    extra_args: list[str] = Field(
        default_factory=list,
        description="More yt-dlp options, one per line (commands and paths are refused)",
    )
    executable: str | None = Field(default=None, description="yt-dlp program (tests only)")

    @field_validator("urls")
    @classmethod
    def _urls(cls, value: list[str]) -> list[str]:
        return check_urls(value)

    @field_validator("extra_args")
    @classmethod
    def _extra_args(cls, value: list[str]) -> list[str]:
        return check_ytdlp_args(value)


def archive_id(extractor_key: str, video_id: str) -> str:
    """yt-dlp's download-archive line (YoutubeDL._make_archive_id / utils.make_archive_id)."""
    return f"{extractor_key.lower()} {video_id}"


def template_path(path: Path) -> str:
    """A literal path inside a yt-dlp output template."""
    return str(path).replace("%", "%%")


def parse_done_line(line: str) -> tuple[str, Path, Path | None] | None:
    """(archive id, media path, info JSON path) from one done.tsv line."""
    parts = line.rsplit("\t", 2)
    if len(parts) != 3:
        return None
    ident, media, info = parts
    key, sep, video_id = ident.partition(" ")
    if not sep or not key or not video_id or not media or media == "NA":
        return None
    return archive_id(key, video_id), Path(media), (Path(info) if info and info != "NA" else None)


def tags_from_info(info: dict[str, Any], config: YtdlpConfig) -> list[str]:
    tags = [t for t in info.get("tags") or [] if isinstance(t, str)]
    if config.include_categories:
        tags += [c for c in info.get("categories") or [] if isinstance(c, str)]
    uploader = info.get("uploader")
    if config.include_uploader_tag and isinstance(uploader, str) and uploader.strip():
        tags.append(f"uploader:{uploader}")
    return tags


def build_args(
    exe: str, ctx: CrawlContext, config: YtdlpConfig, archive: Path, done: Path
) -> list[str]:
    args = [
        exe,
        "--ignore-config",
        "-o",
        template_path(ctx.work_dir) + "/%(extractor_key)s/%(id)s.%(ext)s",
        "-f",
        config.format,
        "--write-info-json",
        "--download-archive",
        str(archive),
        "--no-progress",
        "--print-to-file",
        f"after_move:{DONE_TEMPLATE}",
        template_path(done),
    ]
    if config.cookies is not None:
        args += ["--cookies", str(config.cookies)]
    if config.date_after:
        args += ["--dateafter", config.date_after]
    if config.max_items:
        args += ["--max-downloads", str(config.max_items)]
    return [*args, *config.extra_args, "--", *config.urls]


class YtdlpAddon(Addon):
    key = "ytdlp"
    name = "yt-dlp (video sites)"
    version = "1.0"
    description = "Downloads videos with yt-dlp; tags from the video, its categories and uploader."
    ConfigModel = YtdlpConfig

    async def run(self, ctx: CrawlContext, config: YtdlpConfig) -> None:
        exe = config.executable or str(tool_path("yt-dlp"))
        archive = ctx.state_dir / ARCHIVE_NAME
        known = sorted(await ctx.known_remote_ids())
        atomic_write_bytes(archive, "".join(f"{line}\n" for line in known).encode())
        ctx.log.info("download archive %s: %d known ids from the database", archive, len(known))
        done = ctx.work_dir / DONE_NAME
        tail = watch.LineTail(done)
        emitted_before = ctx.emitted

        async def poll(final: bool) -> None:
            for line in tail.read(final=final):
                await self._emit_line(ctx, config, line)

        result = await watch.run_while_polling(
            ctx,
            build_args(exe, ctx, config, archive, done),
            poll,
            interval=POLL_SECONDS,
            timeout=SUBPROCESS_TIMEOUT,
        )
        emitted = ctx.emitted - emitted_before
        if result.cancelled:
            return
        if result.timed_out:
            raise RuntimeError("yt-dlp timed out")
        if result.returncode in (0, EXIT_MAX_DOWNLOADS):
            ctx.log.info("yt-dlp finished; emitted %d file(s)", emitted)
            return
        if emitted == 0:
            last = next((t for t in reversed(result.tail) if t.strip()), "")
            raise RuntimeError(f"yt-dlp exited with {result.returncode}: {last}")
        ctx.log.warning(
            "yt-dlp exited with %s after %d file(s) were emitted; the run counts as succeeded",
            result.returncode,
            emitted,
        )

    async def _emit_line(self, ctx: CrawlContext, config: YtdlpConfig, line: str) -> None:
        parsed = parse_done_line(line)
        if parsed is None:
            ctx.log.warning("ignoring malformed done.tsv line %r", line)
            return
        remote_id, media, info_path = parsed
        info: dict[str, Any] = {}
        if info_path is not None:
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                ctx.log.warning("no info JSON for %s: %s", remote_id, exc)
        try:
            await ctx.emit(
                CrawledItem(
                    path=media,
                    remote_id=remote_id,
                    url=info.get("webpage_url") or info.get("original_url"),
                    title=info.get("title"),
                    description=info.get("description"),
                    tags=tags_from_info(info, config),
                    metadata={k: info[k] for k in METADATA_KEYS if info.get(k) is not None},
                )
            )
        except (OSError, ValueError) as exc:
            ctx.log.error("could not emit %s (%s): %s", remote_id, media, exc)
