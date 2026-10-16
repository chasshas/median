"""ffprobe and ffmpeg subprocesses (argument lists, never a shell)."""

import asyncio
import json
from pathlib import Path
from typing import Any

FFPROBE_TIMEOUT = 60.0
FFMPEG_TIMEOUT = 120.0


class ToolError(Exception):
    pass


async def run_tool(args: list[str], time_limit: float) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(time_limit):
            stdout, stderr = await proc.communicate()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"{args[0]} timed out after {time_limit:.0f}s") from None
    if proc.returncode != 0:
        message = stderr.decode(errors="replace").strip().splitlines()
        raise ToolError(f"{args[0]} exited with {proc.returncode}: {' | '.join(message[-3:])}")
    return stdout


async def ffprobe(path: Path) -> dict[str, Any]:
    out = await run_tool(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "--",
            str(path),
        ],
        FFPROBE_TIMEOUT,
    )
    return json.loads(out or b"{}")


async def ffmpeg_frame_png(path: Path, *, at_seconds: float | None) -> bytes:
    """The first video stream's frame at a timestamp (or its first frame) as PNG bytes.

    For audio files the first video stream is the embedded cover art, if any.
    """
    args = ["ffmpeg", "-nostdin", "-v", "error"]
    if at_seconds is not None:
        args += ["-ss", f"{at_seconds:.3f}"]
    args += ["-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-an"]
    args += ["-f", "image2pipe", "-c:v", "png", "pipe:1"]
    return await run_tool(args, FFMPEG_TIMEOUT)
