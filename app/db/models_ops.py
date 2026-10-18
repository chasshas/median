"""Operations: settings stored in the database and maintenance schedule state (phase 8)."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AppSetting(Base):
    """A value that overrides an environment default (app.ops.settings)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ScheduleState(Base):
    """When a maintenance schedule last fired (app.ops.schedule)."""

    __tablename__ = "schedule_state"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    last_fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
