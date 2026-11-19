"""Relative periods resolved by the server, never by the model."""

from datetime import datetime, timedelta
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, StrictInt, model_validator

from sidewit.domain.params import ParamType

PeriodKind = Literal["rolling_days", "previous_day", "previous_week", "previous_month"]
MAX_ROLLING_DAYS = 3660
PERIOD_RULE = f"rolling_days takes days from 1 to {MAX_ROLLING_DAYS}; other kinds take null."


class Period(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: PeriodKind
    days: StrictInt | None = None

    @model_validator(mode="after")
    def _days_match_kind(self) -> Self:
        if self.kind == "rolling_days":
            valid = self.days is not None and 1 <= self.days <= MAX_ROLLING_DAYS
        else:
            valid = self.days is None
        if not valid:
            raise ValueError(PERIOD_RULE)
        return self


def period_bounds(
    period: Period, reference_time: datetime, timezone: str, param_type: ParamType
) -> tuple[str, str]:
    """Half-open [start, end) in the Context timezone. Weeks start on Monday."""
    if reference_time.utcoffset() is None:
        raise ValueError("reference_time must be timezone aware.")
    now = reference_time.astimezone(ZoneInfo(timezone))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period.kind == "rolling_days":
        end = midnight if param_type == "date" else now
        start = end - timedelta(days=period.days)
    elif period.kind == "previous_day":
        end, start = midnight, midnight - timedelta(days=1)
    elif period.kind == "previous_week":
        end = midnight - timedelta(days=midnight.weekday())
        start = end - timedelta(days=7)
    else:
        end = midnight.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
    if param_type == "date":
        return start.date().isoformat(), end.date().isoformat()
    return start.isoformat(), end.isoformat()
