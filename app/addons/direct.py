"""The direct add-on: downloads plain file URLs with httpx (no external tool)."""

import asyncio
import contextlib
import email.message
import email.utils
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.crawl.base import Addon, CrawlContext, CrawledItem
from app.storage.paths import safe_filename

CHUNK_SIZE = 1024 * 1024
MEGABYTE = 1024 * 1024
TIMEOUT = httpx.Timeout(60.0, connect=20.0)
USER_AGENT = "Median/1.0 (direct add-on)"
EXTENDED_FILENAME = re.compile(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", re.IGNORECASE)


class TooLarge(Exception):
    pass


class DirectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urls: list[str] = Field(description="File URLs (http or https), one per line")
    tags: list[str] = Field(default_factory=list, description="Tags given to every file")
    max_size_mb: int = Field(
        default=2048, ge=1, le=1_048_576, description="Skip files larger than this (MB)"
    )

    @field_validator("urls")
    @classmethod
    def _urls(cls, value: list[str]) -> list[str]:
        cleaned = [u.strip() for u in value if u.strip()]
        if not cleaned:
            raise ValueError("give at least one URL, one per line")
        for url in cleaned:
            parts = urlsplit(url)
            if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
                raise ValueError(f"{url!r} is not an http(s) URL")
        return cleaned


def normalize_url(url: str) -> str:
    """The remote id: lowercase scheme and host, no fragment; the rest is kept as given."""
    parts = urlsplit(url.strip())
    netloc = parts.netloc
    userinfo, at, hostport = netloc.rpartition("@")
    netloc = f"{userinfo}{at}{hostport.lower()}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, ""))


def filename_from_disposition(header: str | None) -> str | None:
    """The filename of a Content-Disposition header; filename* (RFC 6266/5987) wins."""
    if not header:
        return None
    extended = EXTENDED_FILENAME.search(header)
    if extended:
        charset, value = extended.group(1) or "utf-8", extended.group(2).strip().strip('"')
        with contextlib.suppress(LookupError, UnicodeDecodeError):
            name = unquote(value, encoding=charset, errors="strict")
            if name:
                return name
    msg = email.message.Message()
    msg["content-disposition"] = header
    name = msg.get_param("filename", header="content-disposition")
    if isinstance(name, tuple):  # RFC 2231 continuation
        name = email.utils.collapse_rfc2231_value(name)
    return name or None


def filename_from_url(url: str) -> str | None:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return name or None


class DirectAddon(Addon):
    key = "direct"
    name = "Direct links"
    version = "1.0"
    description = "Downloads plain file URLs (images, videos, archives) with given tags."
    ConfigModel = DirectConfig

    async def run(self, ctx: CrawlContext, config: DirectConfig) -> None:
        limit = config.max_size_mb * MEGABYTE
        errors = 0
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}
        ) as client:
            for n, url in enumerate(config.urls, 1):
                if ctx.is_cancelled():
                    return
                remote_id = normalize_url(url)
                if await ctx.is_known(remote_id):
                    ctx.log.info("known, skipped: %s", remote_id)
                    continue
                try:
                    item = await self._download(ctx, client, url, n, limit)
                except TooLarge as exc:
                    errors += 1
                    ctx.log.error("skipped %s: %s", url, exc)
                    continue
                except (httpx.HTTPError, OSError) as exc:
                    errors += 1
                    ctx.log.error("download of %s failed: %s", url, exc)
                    continue
                if item is None:  # cancelled
                    return
                await ctx.emit(
                    item.model_copy(update={"remote_id": remote_id, "tags": config.tags})
                )
        if errors and ctx.emitted == 0:
            raise RuntimeError(f"all {errors} download(s) failed")

    async def _download(
        self, ctx: CrawlContext, client: httpx.AsyncClient, url: str, n: int, limit: int
    ) -> CrawledItem | None:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length and length.isdigit() and int(length) > limit:
                raise TooLarge(f"{int(length)} bytes is over the limit of {limit} bytes")
            name = safe_filename(
                filename_from_disposition(response.headers.get("content-disposition"))
                or filename_from_url(str(response.url))
                or filename_from_url(url)
                or "download"
            )
            folder = ctx.work_dir / str(n)
            folder.mkdir(parents=True, exist_ok=True)
            part = folder / f".{name}.part"
            size = 0
            fh = await asyncio.to_thread(open, part, "wb")
            try:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    size += len(chunk)
                    if size > limit:
                        raise TooLarge(f"more than the limit of {limit} bytes")
                    await asyncio.to_thread(fh.write, chunk)
                    if ctx.is_cancelled():
                        return None
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            finally:
                fh.close()
            final = folder / name
            os.replace(part, final)
            ctx.log.info("downloaded %s (%d bytes) as %s", url, size, name)
            return CrawledItem(
                path=Path(final),
                url=url,
                filename=name,
                metadata={
                    "content_type": response.headers.get("content-type"),
                    "final_url": str(response.url),
                },
            )
