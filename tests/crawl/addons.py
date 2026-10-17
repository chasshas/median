"""Add-ons used by the crawl tests."""

import asyncio
import enum
from typing import Literal

from pydantic import BaseModel, Field

from app.addons.demo import DemoAddon
from app.crawl.base import Addon, CrawlContext, SecretFile
from app.crawl.registry import Registry


class Colour(enum.StrEnum):
    red = "red"
    blue = "blue"


class EveryFieldConfig(BaseModel):
    title: str = Field(description="Any text")
    limit: int = Field(default=5, ge=1, le=50)
    ratio: float = 0.5
    verbose: bool = False
    colour: Colour = Colour.red
    mode: Literal["fast", "slow"] = "fast"
    keywords: list[str] = Field(default_factory=list)
    nickname: str | None = None
    cookies: SecretFile = None
    extra: dict[str, int] = Field(default_factory=dict)


class EveryFieldAddon(Addon):
    key = "every_field"
    name = "Every field"
    version = "0.1"
    ConfigModel = EveryFieldConfig

    async def run(self, ctx: CrawlContext, config: EveryFieldConfig) -> None:
        ctx.log.info("cookies at %s", config.cookies)
        if config.cookies is not None:
            ctx.log.info("cookie file says %s", config.cookies.read_text().strip())


class SleeperConfig(BaseModel):
    seconds: float = 60


class SleeperAddon(Addon):
    """Runs a subprocess that sleeps (and starts a child that sleeps too)."""

    key = "sleeper"
    name = "Sleeper"
    ConfigModel = SleeperConfig

    async def run(self, ctx: CrawlContext, config: SleeperConfig) -> None:
        result = await ctx.run_subprocess(
            ["sh", "-c", f"sleep {config.seconds} & echo child $!; wait"], timeout=3600
        )
        ctx.log.info("subprocess result %s", result)


class StubbornAddon(Addon):
    """Ignores is_cancelled() (the runner must stop it anyway)."""

    key = "stubborn"
    name = "Stubborn"

    async def run(self, ctx: CrawlContext, config: BaseModel) -> None:
        while True:  # noqa: ASYNC110 - deliberately ignores cancellation
            await asyncio.sleep(0.1)


class FailingAddon(Addon):
    key = "failing"
    name = "Failing"

    async def run(self, ctx: CrawlContext, config: BaseModel) -> None:
        (ctx.work_dir / "partial.bin").write_bytes(b"x")
        raise RuntimeError("the site changed")


def make_registry() -> Registry:
    registry = Registry()
    for addon in (DemoAddon, EveryFieldAddon, SleeperAddon, StubbornAddon, FailingAddon):
        registry.add(addon, f"tests {addon.__name__}")
    return registry
