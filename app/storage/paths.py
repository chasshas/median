"""Path helpers for every location under DATA_DIR."""

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

_UNSAFE = re.compile(r"[^\w.\- ]+", re.UNICODE)
_BATCH_ID = re.compile(r"^(?:[0-9a-f]{32}|run-[0-9]+)$")
MAX_NAME_LENGTH = 150


def new_upload_batch_id() -> str:
    return uuid.uuid4().hex


def crawl_batch_id(job_run_id: int) -> str:
    return f"run-{job_run_id}"


def is_valid_batch_id(batch_id: str) -> bool:
    return bool(_BATCH_ID.match(batch_id))


def safe_filename(name: str) -> str:
    """A file name that is safe inside one directory: no separators, no leading dots."""
    base = Path(name.replace("\\", "/")).name
    cleaned = _UNSAFE.sub("_", base).strip(" .")
    if not cleaned:
        return "file"
    if len(cleaned) > MAX_NAME_LENGTH:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 10:
            cleaned = stem[: MAX_NAME_LENGTH - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:MAX_NAME_LENGTH]
    return cleaned


@dataclass(frozen=True)
class DataPaths:
    root: Path

    # Top-level locations.
    @property
    def staging(self) -> Path:
        return self.root / "staging"

    @property
    def archive(self) -> Path:
        return self.root / "archive"

    @property
    def thumbs(self) -> Path:
        return self.root / "thumbs"

    @property
    def trash(self) -> Path:
        return self.root / "trash"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def addon_state(self) -> Path:
        return self.root / "addon_state"

    @property
    def secrets(self) -> Path:
        return self.root / "secrets"

    @property
    def addons(self) -> Path:
        """Single-file add-ons (DATA_DIR/addons/*.py)."""
        return self.root / "addons"

    @property
    def tools_venv(self) -> Path:
        return self.root / "tools" / "venv"

    @property
    def job_logs(self) -> Path:
        return self.root / "logs" / "jobs"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    # Per-item locations.
    def staging_batch(self, batch_id: str) -> Path:
        if not is_valid_batch_id(batch_id):
            raise ValueError(f"invalid batch id: {batch_id!r}")
        return self.staging / batch_id

    def archive_path(self, sha_hex: str, ext: str | None) -> Path:
        return self.archive / sha_hex[0:2] / sha_hex[2:4] / _with_ext(sha_hex, ext)

    def thumb_path(self, sha_hex: str) -> Path:
        return self.thumbs / sha_hex[0:2] / f"{sha_hex}.webp"

    def trash_path(self, sha_hex: str, ext: str | None) -> Path:
        return self.trash / _with_ext(sha_hex, ext)

    def work_dir(self, job_run_id: int) -> Path:
        return self.work / str(job_run_id)

    def addon_state_dir(self, source_id: int) -> Path:
        return self.addon_state / str(source_id)

    def secrets_dir(self, source_id: int) -> Path:
        return self.secrets / str(source_id)

    def job_log(self, job_run_id: int) -> Path:
        return self.job_logs / f"{job_run_id}.log"

    def original_path(
        self, status: str, staging_path: str | None, sha_hex: str, ext: str | None
    ) -> Path | None:
        """Where a file's original currently lives, by status (None if it has no location)."""
        if status == "archived":
            return self.archive_path(sha_hex, ext)
        if status == "discarded":
            return self.trash_path(sha_hex, ext)
        if staging_path:
            return self.resolve(staging_path)
        return None

    # Relative paths stored in the database (files.staging_path).
    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def resolve(self, relative: str) -> Path:
        """Turn a stored relative path back into an absolute one, refusing to leave DATA_DIR."""
        root = self.root.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"path escapes DATA_DIR: {relative!r}")
        return path


def _with_ext(sha_hex: str, ext: str | None) -> str:
    return f"{sha_hex}.{ext}" if ext else sha_hex


def get_paths() -> DataPaths:
    return DataPaths(get_settings().data_dir)
