from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.config import get_settings

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
templates = Jinja2Templates(directory=WEB_DIR / "templates")


def localtime(value: datetime | None) -> str:
    """A UTC timestamp shown in APP_TIMEZONE."""
    if value is None:
        return ""
    return value.astimezone(ZoneInfo(get_settings().app_timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")


def filesize(value: int | None) -> str:
    if value is None:
        return ""
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def duration(ms: int | None) -> str:
    if not ms:
        return ""
    seconds = ms / 1000
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}:{minutes:02d}:{sec:05.2f}" if hours else f"{minutes}:{sec:05.2f}"


def seconds(value: float | None) -> str:
    """A duration in seconds as 1:02:03 or 2:03."""
    if value is None:
        return ""
    total = int(value)
    hours, rest = divmod(total, 3600)
    minutes, sec = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"


templates.env.filters["localtime"] = localtime
templates.env.filters["seconds"] = seconds
templates.env.filters["filesize"] = filesize
templates.env.filters["duration"] = duration


def with_query(path: str, query: str) -> str:
    return f"{path}?{query}" if query else path


templates.env.filters["with_query"] = with_query
