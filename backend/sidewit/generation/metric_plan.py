"""Small intent DTO: SQL, joins, aggregate and fixed filters are not model outputs."""

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt

from sidewit.domain.context import ContextDefinition
from sidewit.domain.metric import MetricDefinition, Period, field_for
from sidewit.generation.dto import LLMParameter, LLMToolSuggestion
from sidewit.generation.prompts import template

METRIC_PROMPT_VERSION = "metric_plan_v2"


class LLMPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["rolling_days", "previous_day", "previous_week", "previous_month"]
    days: StrictInt | None


class LLMHybridPlan(BaseModel):
    """One call decides the route: a registered metric (server-built SQL) or model-written SQL."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "ambiguous", "unanswerable"]
    reason: str | None
    clarifications: list[str]
    route: Literal["metric", "sql"] | None
    metric_id: str | None
    period: LLMPeriod | None
    statement: str | None
    parameters: list[LLMParameter]
    tool_suggestion: LLMToolSuggestion | None


class LLMMetricPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "ambiguous", "unanswerable"]
    reason: str | None
    clarifications: list[str]
    metric_id: str | None
    parameters: list[LLMParameter]
    period: LLMPeriod | None
    tool_suggestion: LLMToolSuggestion | None


def metric_prompt(
    definition: ContextDefinition,
    timezone: str,
    reference_time: datetime,
    metric_id: str | None,
    period: Period | None,
) -> str:
    context = {
        "metrics": [_metric_json(definition, m) for m in definition.metrics],
        # exclude_none: a rule without a condition has no `condition` key.
        "rules": [r.model_dump(mode="json", exclude_none=True) for r in definition.rules],
        "timezone": timezone,
        "reference_time": reference_time.isoformat(),
        "required_metric_id": metric_id,
        "period_override": period.model_dump() if period else None,
    }
    return template(METRIC_PROMPT_VERSION).substitute(
        context_json=json.dumps(context, ensure_ascii=False)
    )


def _metric_json(definition: ContextDefinition, metric: MetricDefinition) -> dict:
    """The contract plus, for inputs on enum-like fields, the values they accept."""
    data = metric.model_dump(mode="json", by_alias=True)
    for item, param in zip(data["inputs"], metric.inputs, strict=True):
        field = field_for(definition, param.field)
        if field is not None and field.allowed_values:
            item["allowed_values"] = field.allowed_values
    return data
