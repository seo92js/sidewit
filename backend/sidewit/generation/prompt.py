import re
from datetime import datetime
from zoneinfo import ZoneInfo

from sidewit.domain.context import ContextDefinition, ContextField, Rule
from sidewit.domain.issue import Issue
from sidewit.domain.metric import field_type
from sidewit.generation.prompts import template

PROMPT_VERSION = "generate_v7"
# Bookkeeping timestamps rarely define a business period; they never make a period ambiguous.
_AUDIT_FIELD = re.compile(r"(^|_)(updated|modified|deleted|synced|archived)(_at|_on)?$", re.I)


def render_system_prompt(
    definition: ContextDefinition, dialect: str, timezone: str, reference_time: datetime
) -> str:
    local = reference_time.astimezone(ZoneInfo(timezone))
    return template(PROMPT_VERSION).substitute(
        dialect=dialect,
        timezone=timezone,
        reference_time=local.isoformat(),
        reference_weekday=local.strftime("%A"),
        context=render_context(definition),
    )


def render_context(definition: ContextDefinition) -> str:
    lines = ["## Resources"]
    if not definition.resources:
        lines.append("(none)")
    for resource in definition.resources:
        lines.append(
            f"- {resource.name}" + (f": {resource.description}" if resource.description else "")
        )
        for field in resource.fields:
            attrs = [field.type]
            if field.primary_key:
                attrs.append("primary key")
            elif not field.nullable:
                attrs.append("not null")
            line = f"  - {field.name} ({', '.join(attrs)})"
            if field.description:
                line += f": {field.description}"
            if field.allowed_values:
                values = ", ".join(repr(v) for v in field.allowed_values)
                line += f" Allowed values (exact): {values}."
            lines.append(line)
        undescribed_dates = [f.name for f in resource.fields if _period_candidate(f)]
        if len(undescribed_dates) > 1:
            lines.append(
                "  Only for requests needing date-dependent filtering or grouping. "
                "Period choice is unresolved by field descriptions: "
                + ", ".join(undescribed_dates)
                + ". If no relevant rule or explicit user choice settles it, ask which date "
                "to use; do not default to one. Otherwise do not require a date choice."
            )

    lines.append("")
    lines.append("## Relations")
    lines.extend([f"- {r.from_} → {r.to}" for r in definition.relations] or ["(none)"])

    lines.append("")
    lines.append("## Business rules")
    lines.extend([_render_rule(r) for r in definition.rules] or ["(none)"])
    return "\n".join(lines)


HYBRID_PROMPT_VERSION = "hybrid_v2"


def render_hybrid_prompt(
    definition: ContextDefinition, dialect: str, timezone: str, reference_time: datetime
) -> str:
    """generate_v7 (the SQL route) followed by the registered metrics and routing rules."""
    return render_system_prompt(definition, dialect, timezone, reference_time) + template(
        HYBRID_PROMPT_VERSION
    ).substitute(metrics=render_metrics(definition))


def render_metrics(definition: ContextDefinition) -> str:
    lines: list[str] = []
    for metric in definition.metrics:
        aggregate = metric.aggregate
        target = "*" if aggregate.kind == "row_count" else aggregate.field
        function = {"row_count": "COUNT", "non_null_count": "COUNT"}.get(
            aggregate.kind, aggregate.kind.upper()
        )
        distinct = "DISTINCT " if aggregate.distinct else ""
        lines.append(f"- {metric.id}: {metric.description}")
        lines.append(f"  measure: {function}({distinct}{target}) over {metric.resource}")
        if metric.joins:
            joins = ", ".join(f"{j.from_} = {j.to}" for j in metric.joins)
            lines.append(f"  inner joins: {joins}")
        if metric.fixed_filters:
            fixed = " AND ".join(f"{f.field} {f.op} {f.value!r}" for f in metric.fixed_filters)
            lines.append(f"  fixed filters: {fixed}")
        if metric.inputs:
            inputs = ", ".join(
                f"{i.name} ({i.type}): {i.field} {i.op} :{i.name}" for i in metric.inputs
            )
            lines.append(f"  inputs: {inputs}")
        if metric.group_by:
            lines.append(f"  group by: {', '.join(metric.group_by)}")
        if metric.time_fields:
            default = f" (default {metric.default_time_field})" if metric.default_time_field else ""
            lines.append(f"  period fields: {', '.join(metric.time_fields)}{default}")
    return "\n".join(lines) or "(none)"


def render_repair_message(errors: list[Issue]) -> str:
    listed = "\n".join(
        f"- [{e.code}] {e.message}" + (f" (ref: {e.ref})" if e.ref else "") for e in errors
    )
    return (
        "The SQL in your previous response failed SQL or request-parameter validation:\n"
        f"{listed}\n\n"
        "Return a corrected response for the same question, following every rule. "
        "If the data context cannot answer the question, return status `unanswerable` or "
        "`ambiguous` instead of guessing."
    )


def _period_candidate(field: ContextField) -> bool:
    """An undescribed date/time field that could plausibly define a requested period."""
    return (
        not field.description
        and field_type(field) in {"date", "date-time"}
        and not _AUDIT_FIELD.search(field.name)
    )


def _render_rule(rule: Rule) -> str:
    """A rule's constant is appended so the model writes it as a literal."""
    line = f"- [{rule.id}] {rule.text}"
    if rule.condition is not None:
        c = rule.condition
        line += f" (fixed: {c.field} {c.op} {c.value!r}; always a SQL literal, never an input)"
    return line
