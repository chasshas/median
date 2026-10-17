"""The gallery-dl add-on: downloads images from the galleries and sites gallery-dl supports.

gallery-dl runs as a subprocess from the tool venv and writes every file into work_dir/files
with a JSON sidecar (--write-metadata, "<file>.json"). A file is emitted once both exist and the
file's size has not changed for 2 seconds; partial downloads (.part) are ignored.
"""

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.crawl import watch
from app.crawl.base import Addon, CrawlContext, CrawledItem, SecretFile
from app.crawl.safety import check_gallerydl_args, check_urls
from app.crawl.tools import tool_path

ARCHIVE_NAME = "gallery-dl-archive.sqlite3"
FILES_DIR = "files"
SIDECAR_SUFFIX = ".json"
POLL_SECONDS = 2.0
STABLE_SECONDS = 2.0
SUBPROCESS_TIMEOUT = 7 * 24 * 3600.0  # the source's max_runtime_minutes is the real limit
TAG_FIELD = re.compile(r"^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)*(=[a-z0-9_]+)?$")


class GallerydlConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urls: list[str] = Field(description="Gallery, post or user URLs, one per line")
    tag_fields: list[str] = Field(
        default_factory=lambda: ["tags", "artist=artist"],
        description='Metadata fields that hold tags: "field" or "field=namespace";'
        " nested fields use dots (author.name)",
    )
    range: str | None = Field(default=None, description="Which files, e.g. 1-20 (--range)")
    cookies: SecretFile = Field(default=None, description="Netscape cookies.txt")
    extra_args: list[str] = Field(
        default_factory=list,
        description="More gallery-dl options, one per line (commands and paths are refused)",
    )
    executable: str | None = Field(default=None, description="gallery-dl program (tests only)")

    @field_validator("urls")
    @classmethod
    def _urls(cls, value: list[str]) -> list[str]:
        return check_urls(value)

    @field_validator("tag_fields")
    @classmethod
    def _tag_fields(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip() for v in value if v.strip()]
        for item in cleaned:
            if not TAG_FIELD.match(item):
                raise ValueError(f'{item!r} is not "field" or "field=namespace"')
        return cleaned

    @field_validator("range")
    @classmethod
    def _range(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value.strip().startswith("-")):
            raise ValueError("give a range such as 1-20")
        return value.strip() if value is not None else None

    @field_validator("extra_args")
    @classmethod
    def _extra_args(cls, value: list[str]) -> list[str]:
        return check_gallerydl_args(value)


def sidecar_for(media: Path) -> Path:
    return media.with_name(media.name + SIDECAR_SUFFIX)


def field_value(data: dict[str, Any], dotted: str) -> Any:
    value: Any = data
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _words(value: Any) -> list[str]:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str):
        return value.split()
    if isinstance(value, int | float):
        return [str(value)]
    if isinstance(value, list | tuple):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                out.append(item)  # list items are whole tags, spaces included
            elif isinstance(item, int | float) and not isinstance(item, bool):
                out.append(str(item))
        return out
    return []


def tags_from_sidecar(data: dict[str, Any], tag_fields: list[str]) -> list[str]:
    """Tags from the configured fields; "field=ns" turns each value into "ns:value"."""
    tags: list[str] = []
    for spec in tag_fields:
        name, _, namespace = spec.partition("=")
        for word in _words(field_value(data, name)):
            tags.append(f"{namespace}:{word}" if namespace else word)
    return tags


def remote_id_for(data: dict[str, Any], media: Path) -> str:
    """ "<category>:<id>:<num>" ("<category>:<id>" without num), else "<category>:<filename>"."""
    category = data.get("category") or "unknown"
    ident = data.get("id")
    if ident is not None and ident != "":
        num = data.get("num")
        return f"{category}:{ident}:{num}" if num is not None else f"{category}:{ident}"
    filename = data.get("filename") or media.stem
    return f"{category}:{filename}"


def build_args(exe: str, ctx: CrawlContext, config: GallerydlConfig) -> list[str]:
    args = [
        exe,
        "--config-ignore",
        "-D",
        str(ctx.work_dir / FILES_DIR),
        "--write-metadata",
        "--download-archive",
        str(ctx.state_dir / ARCHIVE_NAME),
    ]
    if config.cookies is not None:
        args += ["--cookies", str(config.cookies)]
    if config.range:
        args += ["--range", config.range]
    return [*args, *config.extra_args, "--", *config.urls]


class GallerydlAddon(Addon):
    key = "gallerydl"
    name = "gallery-dl (image galleries)"
    version = "1.0"
    description = "Downloads images with gallery-dl; tags from the chosen metadata fields."
    ConfigModel = GallerydlConfig

    async def run(self, ctx: CrawlContext, config: GallerydlConfig) -> None:
        exe = config.executable or str(tool_path("gallery-dl"))
        files = watch.StableFiles(
            ctx.work_dir / FILES_DIR,
            sidecar_for=sidecar_for,
            stable_seconds=STABLE_SECONDS,
            sidecar_suffixes=(SIDECAR_SUFFIX,),
        )
        emitted_before = ctx.emitted

        async def poll(final: bool) -> None:
            for media in files.ready(final=final):
                await self._emit_file(ctx, config, media)

        result = await watch.run_while_polling(
            ctx,
            build_args(exe, ctx, config),
            poll,
            interval=POLL_SECONDS,
            timeout=SUBPROCESS_TIMEOUT,
        )
        for media in files.leftovers():
            ctx.log.warning("no metadata sidecar for %s; not emitted", media.name)
        emitted = ctx.emitted - emitted_before
        if result.cancelled:
            return
        if result.timed_out:
            raise RuntimeError("gallery-dl timed out")
        if result.returncode == 0:
            ctx.log.info("gallery-dl finished; emitted %d file(s)", emitted)
            return
        if emitted == 0:
            last = next((t for t in reversed(result.tail) if t.strip()), "")
            raise RuntimeError(f"gallery-dl exited with {result.returncode}: {last}")
        ctx.log.warning(
            "gallery-dl exited with %s after %d file(s) were emitted; the run counts as succeeded",
            result.returncode,
            emitted,
        )

    async def _emit_file(self, ctx: CrawlContext, config: GallerydlConfig, media: Path) -> None:
        try:
            data = json.loads(sidecar_for(media).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            ctx.log.error("unreadable sidecar for %s: %s", media.name, exc)
            return
        if not isinstance(data, dict):
            ctx.log.error("sidecar of %s is not a JSON object", media.name)
            return
        remote_id = remote_id_for(data, media)
        url = next(
            (data[k] for k in ("post_url", "page_url", "url") if isinstance(data.get(k), str)),
            None,
        )
        title = data.get("title")
        description = data.get("description")
        try:
            await ctx.emit(
                CrawledItem(
                    path=media,
                    remote_id=remote_id,
                    url=url,
                    title=title if isinstance(title, str) else None,
                    description=description if isinstance(description, str) else None,
                    tags=tags_from_sidecar(data, config.tag_fields),
                    metadata={
                        "category": data.get("category"),
                        "subcategory": data.get("subcategory"),
                        "gallerydl": data,
                    },
                )
            )
        except (OSError, ValueError) as exc:
            ctx.log.error("could not emit %s (%s): %s", remote_id, media.name, exc)
