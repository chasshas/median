"""Database backups: pg_dump in custom format to DATA_DIR/backups/db-<timestamp>.dump, keeping
the newest N, and restore of such a dump into the app database (python -m app.cli restore).

Media are not in the dump: archive/ and thumbs/ are copied separately (make backup-files).
The connection goes to pg_dump and pg_restore through PG* environment variables, so the
password never appears in a process list.
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict

from app.config import libpq_url
from app.storage import DataPaths

logger = logging.getLogger(__name__)

DUMP_NAME = re.compile(r"^db-(\d{8}T\d{6}Z)\.dump$")
DUMP_TIMEOUT_SECONDS = 6 * 3600
LIST_TIMEOUT_SECONDS = 600
_PG_ENV = {
    "host": "PGHOST",
    "port": "PGPORT",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "dbname": "PGDATABASE",
    "sslmode": "PGSSLMODE",
}


class BackupError(RuntimeError):
    pass


def pg_env(database_url: str) -> dict[str, str]:
    """Environment for the PostgreSQL client tools: PATH, locale and the PG* connection."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    for key, value in conninfo_to_dict(libpq_url(database_url)).items():
        if key in _PG_ENV and value is not None:
            env[_PG_ENV[key]] = str(value)
    return env


def client_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise BackupError(f"{name} is not installed")
    return path


def tool_version(name: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [client_tool(name), "--version"], capture_output=True, text=True, timeout=30
        )
    except (BackupError, OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def dump_name(now: datetime) -> str:
    return f"db-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}.dump"


def list_dumps(paths: DataPaths) -> list[Path]:
    """Backup files, newest first (the timestamp in the name sorts chronologically)."""
    if not paths.backups.is_dir():
        return []
    dumps = [p for p in paths.backups.iterdir() if p.is_file() and DUMP_NAME.match(p.name)]
    return sorted(dumps, key=lambda p: p.name, reverse=True)


def apply_retention(paths: DataPaths, keep: int) -> list[str]:
    """Delete all but the newest `keep` dumps. Returns the deleted names."""
    deleted = []
    for old in list_dumps(paths)[keep:]:
        old.unlink(missing_ok=True)
        deleted.append(old.name)
    return deleted


async def _run(args: list[str], env: dict[str, str], limit: float, log: logging.Logger) -> str:
    """Run a client tool (argument list, no shell); its stderr goes to the log."""
    proc = await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), limit)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise BackupError(f"{Path(args[0]).name} timed out after {limit:.0f} s") from exc
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    for line in err.decode(errors="replace").splitlines():
        log.info("%s | %s", Path(args[0]).name, line)
    if proc.returncode != 0:
        raise BackupError(f"{Path(args[0]).name} exited with {proc.returncode}")
    return out.decode(errors="replace")


def _fsync_and_rename(tmp: Path, final: Path) -> None:
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, final)


def count_entries(listing: str) -> int:
    """Table of contents entries in pg_restore --list output (comment lines start with ;)."""
    return sum(1 for line in listing.splitlines() if line.strip() and not line.startswith(";"))


@dataclass
class BackupResult:
    file: str
    size_bytes: int
    entries: int
    deleted: list[str] = field(default_factory=list)
    kept: int = 0
    pg_dump: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "size_bytes": self.size_bytes,
            "entries": self.entries,
            "deleted": self.deleted,
            "kept": self.kept,
            "pg_dump": self.pg_dump,
        }


async def backup_database(
    database_url: str,
    paths: DataPaths,
    keep: int,
    log: logging.Logger,
    now: datetime | None = None,
) -> BackupResult:
    """Dump the database, check that pg_restore can read the dump, then apply retention."""
    env = pg_env(database_url)
    paths.backups.mkdir(parents=True, exist_ok=True)
    final = paths.backups / dump_name(now or datetime.now(UTC))
    tmp = paths.backups / f".{final.name}.part"
    version = tool_version("pg_dump")
    log.info("%s -> %s", version or "pg_dump", final.name)
    try:
        await _run(
            [client_tool("pg_dump"), "--format=custom", "--file", str(tmp)],
            env,
            DUMP_TIMEOUT_SECONDS,
            log,
        )
        listing = await _run(
            [client_tool("pg_restore"), "--list", str(tmp)], env, LIST_TIMEOUT_SECONDS, log
        )
        entries = count_entries(listing)
        if entries == 0:
            raise BackupError("the dump has no entries")
        await asyncio.to_thread(_fsync_and_rename, tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    deleted = apply_retention(paths, keep)
    for name in deleted:
        log.info("deleted old backup %s", name)
    result = BackupResult(
        file=final.relative_to(paths.root).as_posix(),
        size_bytes=final.stat().st_size,
        entries=entries,
        deleted=deleted,
        kept=len(list_dumps(paths)),
        pg_dump=version,
    )
    log.info("backup %s: %d bytes, %d entries", result.file, result.size_bytes, entries)
    return result


# Restore -------------------------------------------------------------------------------------


def _check(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - argument list, no shell
        args, env=env, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise BackupError(
            f"{Path(args[0]).name} exited with {result.returncode}: {result.stderr.strip()}"
        )
    return result


def restore_database(
    database_url: str,
    dump: Path,
    *,
    migrate: Callable[[str], None] | None = None,
    echo: Callable[[str], None] = print,
) -> int:
    """Replace the contents of the database with a custom-format dump. Returns the number of
    restored entries. The dump is checked first; then the public schema is dropped and
    recreated, the dump restored in one transaction, and the migrations applied on top (a dump
    from an older version is brought up to date)."""
    env = pg_env(database_url)
    entries = count_entries(_check([client_tool("pg_restore"), "--list", str(dump)], env).stdout)
    if entries == 0:
        raise BackupError(f"{dump} has no entries")
    echo(f"{dump}: {entries} entries; replacing the database")
    with psycopg.connect(libpq_url(database_url), autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    _check(
        [
            client_tool("pg_restore"),
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            "--single-transaction",
            "--dbname",
            env.get("PGDATABASE", ""),
            str(dump),
        ],
        env,
    )
    echo("restored; applying migrations")
    if migrate is None:
        from app.db.migrate import migrate as migrate_db

        migrate = migrate_db
    migrate(database_url)
    return entries


def spool_stdin(stream: Any, directory: Path) -> Path:
    """Copy a dump read from stdin into a temporary file (pg_restore checks it twice)."""
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".restore-", suffix=".dump", dir=directory)
    with os.fdopen(fd, "wb") as out:
        shutil.copyfileobj(stream, out, 1024 * 1024)
    return Path(name)
