"""Settings that /settings can change: a value in app_settings overrides the environment default.

load_settings(session) reads the table and refreshes a process-wide cache; current() returns
the cached values (environment defaults until the first load) for code without a session,
such as the template filters. The web process loads at startup and after every save; jobs
load at their start, so the worker always sees the latest values.
"""

from dataclasses import dataclass, field, fields
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models_ops import AppSetting


class SettingsValues(BaseModel):
    """Every editable setting with its validation."""

    image_phash_threshold: int = Field(ge=0, le=64)
    video_frame_threshold: int = Field(ge=0, le=64)
    trash_retention_days: int = Field(ge=0, le=3650)
    app_timezone: str
    review_order: Literal["oldest", "newest"]
    backup_retention: int = Field(ge=1, le=1000)
    tools_update_schedule: str

    @field_validator("app_timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @field_validator("tools_update_schedule")
    @classmethod
    def _cron(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value.split()) != 5 or not croniter.is_valid(value):
            raise ValueError("expected a cron expression with 5 fields, e.g. 0 5 * * 1")
        return value


KEYS: tuple[str, ...] = tuple(SettingsValues.model_fields)
# Changing one of these makes the stored similar pairs out of date.
THRESHOLD_KEYS = frozenset({"image_phash_threshold", "video_frame_threshold"})


@dataclass(frozen=True)
class SettingInfo:
    key: str
    label: str
    help: str
    env: str


INFO: tuple[SettingInfo, ...] = (
    SettingInfo(
        "image_phash_threshold",
        "Image similarity threshold",
        "Maximum Hamming distance (0-64) between two image pHashes to flag them as similar.",
        "IMAGE_PHASH_THRESHOLD",
    ),
    SettingInfo(
        "video_frame_threshold",
        "Video frame threshold",
        "Maximum Hamming distance (0-64) per video frame pair; 3 of 5 frames must match.",
        "VIDEO_FRAME_THRESHOLD",
    ),
    SettingInfo(
        "trash_retention_days",
        "Trash retention (days)",
        "Discarded originals stay in trash/ this long, so a discard can be undone.",
        "TRASH_RETENTION_DAYS",
    ),
    SettingInfo(
        "app_timezone",
        "Time zone",
        "IANA name (e.g. Asia/Seoul). Times are shown, and schedules run, in this zone.",
        "APP_TIMEZONE",
    ),
    SettingInfo(
        "review_order",
        "Default review order",
        "Which pending files the review queue shows first.",
        "REVIEW_ORDER",
    ),
    SettingInfo(
        "backup_retention",
        "Database backups to keep",
        "The backup job deletes older dumps beyond this many.",
        "BACKUP_RETENTION",
    ),
    SettingInfo(
        "tools_update_schedule",
        "Crawler tool update schedule",
        "Cron expression (minute hour day month weekday) in the time zone above.",
        "TOOLS_UPDATE_SCHEDULE",
    ),
)


@dataclass(frozen=True)
class EffectiveSettings:
    image_phash_threshold: int
    video_frame_threshold: int
    trash_retention_days: int
    app_timezone: str
    review_order: Literal["oldest", "newest"]
    backup_retention: int
    tools_update_schedule: str
    overridden: frozenset[str] = field(default=frozenset())

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    def values(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "overridden"}


class SettingsError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def env_defaults(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    return {key: getattr(settings, key) for key in KEYS}


def _effective(stored: dict[str, Any], settings: Settings | None = None) -> EffectiveSettings:
    values = env_defaults(settings)
    overridden = set()
    for key, value in stored.items():
        if key not in KEYS:
            continue
        candidate = {**values, key: value}
        try:
            SettingsValues.model_validate(candidate)
        except ValidationError:
            continue  # an invalid stored value (edited by hand) falls back to the default
        values[key] = value
        overridden.add(key)
    return EffectiveSettings(**values, overridden=frozenset(overridden))


_cache: EffectiveSettings | None = None


def current() -> EffectiveSettings:
    """The last loaded settings (environment defaults before the first load)."""
    return _cache if _cache is not None else _effective({})


def current_timezone() -> ZoneInfo:
    return current().zone


def reset_cache() -> None:
    global _cache
    _cache = None


async def stored_values(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(select(AppSetting.key, AppSetting.value))
    return {key: value for key, value in rows.all()}


async def load_settings(session: AsyncSession) -> EffectiveSettings:
    """Read app_settings, refresh the cache and return the effective settings."""
    global _cache
    _cache = _effective(await stored_values(session))
    return _cache


def parse_form(form: dict[str, str]) -> dict[str, Any]:
    """Validate submitted values (strings from a form) into typed values; SettingsError."""
    base = current().values()
    submitted = {k: v.strip() for k, v in form.items() if k in KEYS}
    try:
        model = SettingsValues.model_validate({**base, **submitted})
    except ValidationError as exc:
        errors = {}
        for err in exc.errors():
            key = str(err["loc"][0]) if err["loc"] else "form"
            errors[key] = str(err["msg"]).removeprefix("Value error, ")
        raise SettingsError(errors) from exc
    return {k: getattr(model, k) for k in submitted}


async def save_settings(session: AsyncSession, changes: dict[str, Any]) -> set[str]:
    """Store validated values and commit. A value equal to the environment default removes
    the override. Returns the keys whose effective value changed."""
    before = (await load_settings(session)).values()
    validated = SettingsValues.model_validate({**before, **changes})
    defaults = env_defaults()
    for key in changes:
        value = getattr(validated, key)
        if value == defaults[key]:
            await session.execute(delete(AppSetting).where(AppSetting.key == key))
        else:
            stmt = insert(AppSetting).values(key=key, value=value)
            stmt = stmt.on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
            )
            await session.execute(stmt)
    await session.commit()
    after = (await load_settings(session)).values()
    return {k for k in KEYS if before[k] != after[k]}
