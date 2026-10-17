"""Autocomplete for the query box: suggestions for the token being typed.

suggest(token) returns replacements for the whole token: keys, tags (also after -, ~ and +),
folder paths after folder: and folder=:, and the values of type:, status: and source:.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_files import FileSource, MediaKind
from app.library.folders import folder_choices
from app.library.tags import autocomplete
from app.search.query import KEY_PATTERN, PREFIXES, STATUS_WORDS, quote, tag_token, value_token

KEY_HINTS: dict[str, str] = {
    "folder:": "folder with subfolders",
    "folder=:": "exactly this folder",
    "collected:": "collected date range, YYYY-MM-DD..YYYY-MM-DD",
    "archived:": "archived date range, YYYY-MM-DD..YYYY-MM-DD",
    "type:": "image, video, audio or other",
    "source:": "add-on key or upload",
    "status:": "pending, archived or discarded",
}


@dataclass(frozen=True)
class Suggestion:
    value: str  # the replacement for the whole token
    label: str

    def as_json(self) -> dict[str, str]:
        return {"value": self.value, "label": self.label}


async def _tags(session: AsyncSession, prefix: str, text: str, limit: int) -> list[Suggestion]:
    if not text:
        return []
    return [
        Suggestion(tag_token(prefix, t.name), f"{prefix}{t.name} ({t.count})")
        for t in await autocomplete(session, text, limit)
    ]


async def suggest(session: AsyncSession, token: str, limit: int = 10) -> list[Suggestion]:
    if not token:
        return [Suggestion(k, f"{k} {hint}") for k, hint in KEY_HINTS.items()][:limit]
    if token[0] in PREFIXES:
        return await _tags(session, token[0], token[1:].lstrip('"'), limit)
    match = KEY_PATTERN.match(token)
    if match is None:
        keys = [
            Suggestion(k, f"{k} {hint}")
            for k, hint in KEY_HINTS.items()
            if k.startswith(token.lower())
        ]
        return (keys + await _tags(session, "", token, limit))[:limit]
    key = match.group(0).lower()
    value = token[len(key) :].strip('"').lower()
    if key in ("folder:", "folder=:"):
        out = []
        for choice in await folder_choices(session):
            path = "/".join(choice.path.split(" / "))
            if value in path.lower():
                out.append(Suggestion(f"{key}{quote(path)}", choice.path))
        out.sort(key=lambda s: (not s.label.lower().startswith(value), s.label.lower()))
        return out[:limit]
    if key == "type:":
        words = [k.value for k in MediaKind]
    elif key == "status:":
        words = list(STATUS_WORDS)
    elif key == "source:":
        rows = await session.scalars(
            select(FileSource.source_key).distinct().order_by(FileSource.source_key)
        )
        words = list(rows.all())
    else:
        return []
    return [
        Suggestion(f"{key}{value_token(w)}", f"{key}{w}")
        for w in words
        if w.lower().startswith(value)
    ][:limit]
