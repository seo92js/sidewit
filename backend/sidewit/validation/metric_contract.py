"""Deterministic SQL compilation and validation of developer-owned metric contracts.

This layer has no provider, model output or database dependency.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from sidewit.domain.context import ContextDefinition
from sidewit.domain.dialect import Dialect, identifier, render_sql, to_sqlglot
from sidewit.domain.issue import Issue
from sidewit.domain.metric import MetricBinding, MetricDefinition, Period, field_for, field_type
from sidewit.domain.params import ParamType
from sidewit.sql import parse_sql

_COMPARISONS = {"=": exp.EQ, "!=": exp.NEQ, ">": exp.GT, ">=": exp.GTE, "<": exp.LT, "<=": exp.LTE}


def canonical_ref(definition: ContextDefinition, ref: str) -> str:
    resource_name, _, field_name = ref.partition(".")
    resource = definition.find_resource(resource_name)
    field = field_for(definition, ref)
    if resource is None or field is None:
        raise ValueError("Unknown metric field.")
    return f"{resource.name}.{field.name}"


def _column(definition: ContextDefinition, ref: str, dialect: Dialect) -> exp.Column:
    resource, field = canonical_ref(definition, ref).split(".")
    return exp.Column(this=identifier(field, dialect), table=identifier(resource, dialect))


def _table(name: str, dialect: Dialect) -> exp.Table:
    return exp.Table(this=identifier(name, dialect))


def metric_template(
    definition: ContextDefinition,
    metric: MetricDefinition,
    time_field: str | None,
    dialect: Dialect,
) -> tuple[str, dict[str, ParamType]]:
    """Canonical SQL + required parameters; trusted input only, no runtime values.
    Identifiers are quoted only when the database would otherwise resolve a different name."""
    aggregate = metric.aggregate
    target = _column(definition, aggregate.field, dialect) if aggregate.field else exp.Star()
    if aggregate.distinct:
        target = exp.Distinct(expressions=[target])
    function = {
        "row_count": exp.Count,
        "non_null_count": exp.Count,
        "sum": exp.Sum,
        "avg": exp.Avg,
        "min": exp.Min,
        "max": exp.Max,
    }[aggregate.kind](this=target)
    groups = [_column(definition, ref, dialect) for ref in metric.group_by]
    root = definition.find_resource(metric.resource)
    if root is None:
        raise ValueError("Unknown metric resource.")
    query = exp.select(*groups, exp.alias_(function, "value")).from_(_table(root.name, dialect))
    available = {root.name.lower()}
    for join in metric.joins:
        left, right = (ref.partition(".")[0] for ref in (join.from_, join.to))
        resource = definition.find_resource(right if left.lower() in available else left)
        if resource is None:
            raise ValueError("Unknown metric join resource.")
        query = query.join(
            _table(resource.name, dialect),
            on=exp.EQ(
                this=_column(definition, join.from_, dialect),
                expression=_column(definition, join.to, dialect),
            ),
            join_type="INNER",
        )
        available.add(resource.name.lower())
    predicates: list[exp.Expression] = []
    types: dict[str, ParamType] = {}
    for fixed in metric.fixed_filters:
        predicates.append(
            _COMPARISONS[fixed.op](
                this=_column(definition, fixed.field, dialect),
                expression=exp.convert(fixed.value),
            )
        )
    for param in metric.inputs:
        predicates.append(
            _COMPARISONS[param.op](
                this=_column(definition, param.field, dialect),
                expression=exp.Placeholder(this=param.name),
            )
        )
        types[param.name] = param.type
    if time_field is not None:
        canonical = canonical_ref(definition, time_field)
        if canonical.lower() not in {r.lower() for r in metric.time_fields}:
            raise ValueError("Time field is not allowed by this metric.")
        field = field_for(definition, canonical)
        kind = field_type(field) if field else None
        if kind not in {"date", "date-time"}:
            raise ValueError("Unsupported time field type.")
        for name, comparison in (("start_at", exp.GTE), ("end_at", exp.LT)):
            predicates.append(
                comparison(
                    this=_column(definition, canonical, dialect),
                    expression=exp.Placeholder(this=name),
                )
            )
            types[name] = kind
    if predicates:
        query = query.where(*predicates)
    if groups:
        query = query.group_by(*[g.copy() for g in groups])
    return render_sql(query, dialect), types


def period_bounds(
    period: Period, reference_time: datetime, timezone: str, kind: ParamType
) -> tuple[str, str]:
    if reference_time.utcoffset() is None:
        raise ValueError("reference_time must be timezone aware.")
    now = reference_time.astimezone(ZoneInfo(timezone))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period.kind == "rolling_days":
        if period.days is None:
            raise ValueError("rolling_days requires days.")
        end = midnight if kind == "date" else now
        start = end - timedelta(days=period.days)
    else:
        if period.days is not None:
            raise ValueError("Calendar periods require days=null.")
        if period.kind == "previous_day":
            end, start = midnight, midnight - timedelta(days=1)
        elif period.kind == "previous_week":
            end = midnight - timedelta(days=midnight.weekday())
            start = end - timedelta(days=7)
        else:
            end = midnight.replace(day=1)
            start = (end - timedelta(days=1)).replace(day=1)
    if kind == "date":
        return start.date().isoformat(), end.date().isoformat()
    return start.isoformat(), end.isoformat()


def binding_issues(
    definition: ContextDefinition,
    binding: MetricBinding,
    statement: str,
    types: dict[str, ParamType],
    dialect: Dialect,
) -> list[Issue]:
    """Fail closed on contract edits. Accept formatting, comments and quoting that resolves to
    the same names; reject SQL rewrites. The caller's definition is already integrity-checked
    (Context save rejects invalid contracts)."""
    metric = next((m for m in definition.metrics if m.id == binding.metric_id), None)
    message = "Tool SQL and input types must match its current metric contract."
    try:
        if metric is None:
            raise ValueError("Missing metric.")
        expected, required = metric_template(definition, metric, binding.time_field, dialect)
        if _canonical(statement, dialect) != _canonical(expected, dialect) or types != required:
            raise ValueError(message)
    except (ValueError, sqlglot.errors.SqlglotError, RecursionError):
        return [Issue(code="metric_contract_mismatch", message=message, ref=binding.metric_id)]
    return []


def _canonical(statement: str, dialect: Dialect) -> exp.Expression:
    """AST with comments removed and every identifier reduced to the name it resolves to."""
    statements = parse_sql(statement, to_sqlglot(dialect))
    if len(statements) != 1 or statements[0] is None:
        raise ValueError("Expected one statement.")
    tree = normalize_identifiers(statements[0], dialect=to_sqlglot(dialect))
    for node in tree.walk():
        node.comments = None
        if isinstance(node, exp.Identifier):
            node.set("quoted", False)
    return tree
