"""Streaming multipart/form-data parser: file parts go straight to disk, never into memory."""

import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from io import BufferedWriter
from pathlib import Path

from python_multipart.multipart import MultipartParser, parse_options_header

MAX_FIELD_BYTES = 64 * 1024


class MultipartError(ValueError):
    pass


@dataclass
class ReceivedFile:
    field: str
    filename: str
    path: Path
    size: int = 0


@dataclass
class StreamedForm:
    fields: dict[str, list[str]] = field(default_factory=dict)
    files: list[ReceivedFile] = field(default_factory=list)

    def first(self, name: str, default: str = "") -> str:
        values = self.fields.get(name)
        return values[0] if values else default

    def cleanup(self) -> None:
        for received in self.files:
            with contextlib.suppress(FileNotFoundError):
                received.path.unlink()


class _Collector:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.form = StreamedForm()
        self._headers: dict[str, str] = {}
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._name = ""
        self._buffer = bytearray()
        self._file: ReceivedFile | None = None
        self._handle: BufferedWriter | None = None
        self._skip = False

    def on_part_begin(self) -> None:
        self._headers = {}
        self._name = ""
        self._buffer = bytearray()
        self._file = None
        self._handle = None
        self._skip = False

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        key = self._header_field.decode("latin-1").lower()
        self._headers[key] = self._header_value.decode("utf-8", errors="replace")
        self._header_field = bytearray()
        self._header_value = bytearray()

    def on_headers_finished(self) -> None:
        _, params = parse_options_header(self._headers.get("content-disposition", ""))
        self._name = params.get(b"name", b"").decode("utf-8", errors="replace")
        if b"filename" not in params:
            return
        filename = params[b"filename"].decode("utf-8", errors="replace")
        if not filename:  # an empty file input
            self._skip = True
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f".upload-{uuid.uuid4().hex}.part"
        self._file = ReceivedFile(self._name, filename, path)
        self._handle = open(path, "xb")  # noqa: SIM115 - closed in on_part_end/close
        self.form.files.append(self._file)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._skip:
            return
        if self._handle is not None and self._file is not None:
            self._handle.write(data[start:end])
            self._file.size += end - start
            return
        self._buffer += data[start:end]
        if len(self._buffer) > MAX_FIELD_BYTES:
            raise MultipartError(f"form field {self._name!r} is too large")

    def on_part_end(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        elif not self._skip:
            value = self._buffer.decode("utf-8", errors="replace")
            self.form.fields.setdefault(self._name, []).append(value)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


async def stream_multipart(
    content_type: str, chunks: AsyncIterator[bytes], directory: Path
) -> StreamedForm:
    """Parse a multipart body chunk by chunk, writing file parts into directory."""
    mime, params = parse_options_header(content_type)
    boundary = params.get(b"boundary")
    if mime != b"multipart/form-data" or not boundary:
        raise MultipartError("expected multipart/form-data with a boundary")
    collector = _Collector(directory)
    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": collector.on_part_begin,
            "on_header_field": collector.on_header_field,
            "on_header_value": collector.on_header_value,
            "on_header_end": collector.on_header_end,
            "on_headers_finished": collector.on_headers_finished,
            "on_part_data": collector.on_part_data,
            "on_part_end": collector.on_part_end,
        },
    )
    try:
        async for chunk in chunks:
            parser.write(chunk)
        parser.finalize()
    except Exception as exc:
        collector.close()
        collector.form.cleanup()
        if isinstance(exc, MultipartError):
            raise
        raise MultipartError(f"malformed multipart body: {exc}") from exc
    collector.close()
    return collector.form
