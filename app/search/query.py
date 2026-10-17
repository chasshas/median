"""The search query syntax of ARCHITECTURE.md section 12: parse(query) and format(filter).

    cat -dog ~red ~blue folder:"Animals/Cats" folder=:"Inbox" collected:2026-01-01..2026-03-31
    archived:..2026-05-01 type:image source:ytdlp status:pending "quoted words"

Tokens are separated by whitespace; double quotes group text (with \\" and \\\\ escapes inside).
A token that is a single quoted string is a text search phrase. "-tag" must not have, "~tag" is
any of, "+tag" must have (the explicit form of a bare tag, for tags that look like a key, such
as "+type:image"); after one of those prefixes the rest is always a tag. Keys are case-insensitive.
Repeated type:, source: and status: tokens are alternatives; folder, collected and archived may
appear once. Without status: the search covers archived files. Errors carry the character
position (0-based) in the query; unknown tags are warnings and match nothing.
"""

import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_files import MediaKind
from app.library.folders import FolderNotFound, path_of, resolve_path
from app.library.tags import normalize_tag
from app.search.filters import SearchFilter, SearchStatus, tag_ids

KEYS = ("folder=", "folder", "collected", "archived", "type", "source", "status")
KEY_PATTERN = re.compile(r"(folder=|folder|collected|archived|type|source|status):", re.IGNORECASE)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
STATUS_WORDS: dict[str, SearchStatus] = {
    "pending": "pending_review",
    "archived": "archived",
    "discarded": "discarded",
}
STATUS_KEYWORDS: dict[SearchStatus, str] = {v: k for k, v in STATUS_WORDS.items()}
PREFIXES = {"-": "tags_none", "~": "tags_any", "+": "tags_all"}


class SearchSyntaxError(ValueError):
    def __init__(self, message: str, position: int) -> None:
        super().__init__(f"{message} (at character {position + 1})")
        self.message = message
        self.position = position

    def as_json(self) -> dict[str, object]:
        return {"message": self.message, "position": self.position}


@dataclass(frozen=True)
class SearchWarning:
    message: str
    position: int | None  # None for filters given as fields

    def as_json(self) -> dict[str, object]:
        return {"message": self.message, "position": self.position}


@dataclass
class Parsed:
    filter: SearchFilter
    warnings: list[SearchWarning] = field(default_factory=list)


@dataclass
class _Token:
    start: int
    chars: list[tuple[str, bool, int]]  # (character, inside quotes, position in the query)
    opened_with_quote: bool

    @property
    def whole_quoted(self) -> bool:
        return self.opened_with_quote and all(quoted for _, quoted, _ in self.chars)

    def text(self, offset: int = 0) -> str:
        return "".join(c for c, _, _ in self.chars[offset:])

    def raw_prefix(self) -> str:
        """The leading characters that are outside quotes."""
        out = []
        for c, quoted, _ in self.chars:
            if quoted:
                break
            out.append(c)
        return "".join(out)

    def position(self, offset: int) -> int:
        if offset < len(self.chars):
            return self.chars[offset][2]
        return self.chars[-1][2] + 1 if self.chars else self.start


def tokenize(query: str) -> list[_Token]:
    tokens: list[_Token] = []
    i, n = 0, len(query)
    while i < n:
        if query[i].isspace():
            i += 1
            continue
        token = _Token(i, [], query[i] == '"')
        while i < n and not query[i].isspace():
            if query[i] != '"':
                token.chars.append((query[i], False, i))
                i += 1
                continue
            opened = i
            i += 1
            while True:
                if i >= n:
                    raise SearchSyntaxError("unterminated quote", opened)
                c = query[i]
                if c == '"':
                    i += 1
                    break
                if c == "\\" and i + 1 < n and query[i + 1] in '"\\':
                    token.chars.append((query[i + 1], True, i + 1))
                    i += 2
                    continue
                token.chars.append((c, True, i))
                i += 1
        tokens.append(token)
    return tokens


def _parse_date(text: str, position: int) -> date:
    if not _DATE.fullmatch(text):
        raise SearchSyntaxError(f'"{text}" is not a date (use YYYY-MM-DD)', position)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise SearchSyntaxError(f'"{text}" is not a valid date', position) from exc


def _parse_range(text: str, position: int) -> tuple[date | None, date | None]:
    if ".." not in text:
        day = _parse_date(text, position)
        return day, day
    start_text, end_text = text.split("..", 1)
    if not start_text and not end_text:
        raise SearchSyntaxError("a date range needs a start or an end", position)
    start = _parse_date(start_text, position) if start_text else None
    end = _parse_date(end_text, position + len(start_text) + 2) if end_text else None
    if start and end and start > end:
        raise SearchSyntaxError("the range ends before it starts", position)
    return start, end


async def parse(session: AsyncSession, query: str) -> Parsed:
    """Parse a query string into a SearchFilter (sort and seed are left at their defaults)."""
    values: dict[str, object] = {
        "tags_all": [],
        "tags_any": [],
        "tags_none": [],
        "media_kinds": [],
        "source_keys": [],
        "statuses": [],
        "text": [],
    }
    tag_positions: dict[str, int] = {}
    seen: dict[str, int] = {}

    def once(name: str, position: int) -> None:
        if name in seen:
            raise SearchSyntaxError(f"{name} can be given only once", position)
        seen[name] = position

    for token in tokenize(query):
        if token.whole_quoted:
            if token.text().strip():
                values["text"].append(token.text())  # type: ignore[attr-defined]
            continue
        first = token.chars[0]
        if not first[1] and first[0] in PREFIXES:
            if len(token.chars) == 1:
                raise SearchSyntaxError(f'a tag must follow "{first[0]}"', token.start)
            _add_tag(values, tag_positions, PREFIXES[first[0]], token, 1)
            continue
        match = KEY_PATTERN.match(token.raw_prefix())
        if match is None:
            _add_tag(values, tag_positions, "tags_all", token, 0)
            continue
        key = match.group(1).lower()
        offset = len(match.group(0))
        value = token.text(offset)
        at = token.position(offset)
        if not value:
            raise SearchSyntaxError(f"{key}: needs a value", at)
        if key in ("folder", "folder="):
            once("folder", token.start)
            folder_id = await resolve_path(session, value)
            if folder_id is None:
                raise SearchSyntaxError(f'unknown folder "{value}"', at)
            values["folder_id"] = folder_id
            values["folder_path"] = await folder_query_path(session, folder_id)
            values["include_subfolders"] = key == "folder"
        elif key in ("collected", "archived"):
            once(key, token.start)
            values[f"{key}_from"], values[f"{key}_to"] = _parse_range(value, at)
        elif key == "type":
            try:
                values["media_kinds"].append(MediaKind(value.lower()))  # type: ignore[attr-defined]
            except ValueError as exc:
                allowed = "|".join(k.value for k in MediaKind)
                raise SearchSyntaxError(f'unknown type "{value}" (use {allowed})', at) from exc
        elif key == "status":
            status = STATUS_WORDS.get(value.lower())
            if status is None:
                allowed = "|".join(STATUS_WORDS)
                raise SearchSyntaxError(f'unknown status "{value}" (use {allowed})', at)
            values["statuses"].append(status)  # type: ignore[attr-defined]
        else:
            values["source_keys"].append(value)  # type: ignore[attr-defined]

    flt = SearchFilter.model_validate(values)
    known = await tag_ids(session, tag_positions)
    warnings = [
        SearchWarning(f'no file has the tag "{name}"', position)
        for name, position in tag_positions.items()
        if name not in known
    ]
    return Parsed(flt, warnings)


def _add_tag(
    values: dict[str, object],
    positions: dict[str, int],
    group: str,
    token: _Token,
    offset: int,
) -> None:
    raw = token.text(offset)
    tag = normalize_tag(raw)
    if tag is None:
        raise SearchSyntaxError(f'"{raw}" is not a valid tag', token.position(offset))
    values[group].append(tag)  # type: ignore[attr-defined]
    positions.setdefault(tag, token.position(offset))


async def folder_query_path(session: AsyncSession, folder_id: int) -> str:
    """The folder's path as the query syntax writes it ("A/B")."""
    return "/".join((await path_of(session, folder_id)).split(" / "))


async def with_folder_path(session: AsyncSession, flt: SearchFilter) -> SearchFilter:
    """Fill folder_path from folder_id (filters built from form fields carry only the id)."""
    if flt.folder_id is None:
        return flt
    try:
        path = await folder_query_path(session, flt.folder_id)
    except FolderNotFound:
        path = None
    return flt.model_copy(update={"folder_path": path})


# --- format ----------------------------------------------------------------------------------


def quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def needs_quotes(text: str) -> bool:
    return not text or any(c.isspace() or c in '"\\' for c in text)


def value_token(text: str) -> str:
    return quote(text) if needs_quotes(text) else text


def tag_token(prefix: str, tag: str) -> str:
    if not prefix and (tag[0] in "-~+" or KEY_PATTERN.match(tag)):
        prefix = "+"
    return prefix + value_token(tag)


def _range(start: date | None, end: date | None) -> str:
    if start and start == end:
        return start.isoformat()
    return f"{start.isoformat() if start else ''}..{end.isoformat() if end else ''}"


def format_query(flt: SearchFilter) -> str:
    """The query string for flt; parse(format_query(f)) has the same conditions as f.

    The folder is written from folder_path (see with_folder_path); sort and seed are not part
    of the query string.
    """
    parts = [tag_token("", t) for t in flt.tags_all]
    parts += [tag_token("~", t) for t in flt.tags_any]
    parts += [tag_token("-", t) for t in flt.tags_none]
    if flt.folder_id is not None and flt.folder_path:
        key = "folder" if flt.include_subfolders else "folder="
        parts.append(f"{key}:{quote(flt.folder_path)}")
    if flt.collected_from or flt.collected_to:
        parts.append(f"collected:{_range(flt.collected_from, flt.collected_to)}")
    if flt.archived_from or flt.archived_to:
        parts.append(f"archived:{_range(flt.archived_from, flt.archived_to)}")
    parts += [f"type:{k.value}" for k in flt.media_kinds]
    parts += [f"source:{value_token(s)}" for s in flt.source_keys]
    if flt.statuses != ["archived"]:
        parts += [f"status:{STATUS_KEYWORDS[s]}" for s in flt.statuses]
    parts += [quote(t) for t in flt.text]
    return " ".join(parts)
