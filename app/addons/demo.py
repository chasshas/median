"""The demo add-on: generates structured images. Reference implementation for add-on authors.

It shows the whole contract: a Pydantic config model, run(ctx, config), files written into
ctx.work_dir, stable remote ids (so recrawls are skipped as known remotes before hashing),
ctx.emit() with tags and metadata, ctx.log, and cooperative cancellation.
"""

import asyncio
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field

from app.crawl.base import Addon, CrawlContext, CrawledItem

IMAGE_SIZE = (320, 240)


class DemoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(default=10, ge=1, le=10_000, description="Number of images to generate")
    seed: int = Field(default=0, description="Same seed, same images and remote ids")
    tags: list[str] = Field(default_factory=list, description="Tags given to every image")
    delay_seconds: float = Field(
        default=0.0, ge=0, le=3600, description="Pause before each image (to try cancelling)"
    )


def draw_image(seed: int, n: int, size: tuple[int, int] = IMAGE_SIZE) -> Image.Image:
    """A deterministic image with real structure: gradient, shapes and a label."""
    rng = random.Random(f"demo-{seed}-{n}")  # noqa: S311 - test pictures, not secrets
    w, h = size

    def color() -> tuple[int, int, int]:
        return (rng.randrange(256), rng.randrange(256), rng.randrange(256))

    start, end = color(), color()
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    for x in range(w):
        t = x / max(w - 1, 1)
        draw.line([(x, 0), (x, h)], fill=tuple(int(a + (b - a) * t) for a, b in zip(start, end)))  # noqa: B905
    for _ in range(12):
        x0, y0 = rng.randrange(w), rng.randrange(h)
        x1, y1 = rng.randrange(x0, w + 1), rng.randrange(y0, h + 1)
        kind = rng.randrange(3)
        if kind == 0:
            draw.ellipse([x0, y0, x1, y1], fill=color())
        elif kind == 1:
            draw.rectangle([x0, y0, x1, y1], fill=color())
        else:
            draw.line([(x0, y0), (x1, y1)], fill=color(), width=rng.randrange(3, 16))
    font = ImageFont.load_default(size=28)
    draw.text((10, h - 44), f"demo {seed}/{n}", fill=color(), font=font)
    return img


def write_image(path: Path, seed: int, n: int) -> None:
    draw_image(seed, n).save(path, "PNG")


class DemoAddon(Addon):
    key = "demo"
    name = "Demo images"
    version = "1.0"
    description = "Generates structured test images with Pillow (no network)."
    ConfigModel = DemoConfig

    async def run(self, ctx: CrawlContext, config: DemoConfig) -> None:
        ctx.log.info("generating %d images with seed %d", config.count, config.seed)
        for n in range(1, config.count + 1):
            if config.delay_seconds and await ctx.sleep(config.delay_seconds):
                break
            if ctx.is_cancelled():
                break
            path = ctx.work_dir / f"demo-{config.seed}-{n}.png"
            await asyncio.to_thread(write_image, path, config.seed, n)
            await ctx.emit(
                CrawledItem(
                    path=path,
                    remote_id=f"demo-{config.seed}-{n}",
                    url=f"demo://{config.seed}/{n}",
                    title=f"Demo image {n} (seed {config.seed})",
                    tags=config.tags,
                    metadata={"seed": config.seed, "n": n},
                )
            )
        ctx.log.info("emitted %d images", ctx.emitted)
