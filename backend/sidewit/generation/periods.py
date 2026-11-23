"""Fill period bound parameters from the model's period intent (SPEC §6.2)."""

from dataclasses import dataclass, field
from datetime import datetime

from sidewit.domain.issue import Issue
from sidewit.domain.params import DATE_TYPES, ParamType
from sidewit.domain.period import PERIOD_RULE, period_bounds
from sidewit.generation.dto import LLMPeriodBounds


@dataclass
class BoundPeriods:
    values: dict[str, str] = field(default_factory=dict)
    parameters: tuple[tuple[str, str], ...] = ()  # (start, end) names
    errors: list[Issue] = field(default_factory=list)


def bind_periods(
    periods: list[LLMPeriodBounds],
    types: dict[str, ParamType],
    reference_time: datetime,
    timezone: str,
) -> BoundPeriods:
    """Server values replace whatever the model wrote for these parameters."""
    result = BoundPeriods()
    seen: set[str] = set()

    def error(message: str, ref: str) -> None:
        result.errors.append(Issue(code="period_invalid", message=message, ref=ref))

    for item in periods:
        names = (item.start, item.end)
        if item.start == item.end or seen.intersection(names):
            error("Each period needs its own two distinct parameters.", item.start)
            continue
        seen.update(names)
        kinds = {types.get(name) for name in names}
        if len(kinds) != 1 or not kinds <= DATE_TYPES:
            error(
                f"Period parameters '{item.start}' and '{item.end}' must both be declared, "
                "with the same type: date or date-time.",
                item.start,
            )
            continue
        try:
            start, end = period_bounds(item.period(), reference_time, timezone, kinds.pop())
        except (ValueError, OverflowError):  # includes pydantic.ValidationError
            error(PERIOD_RULE, item.start)
            continue
        result.values[item.start], result.values[item.end] = start, end
        result.parameters += (names,)
    return result
