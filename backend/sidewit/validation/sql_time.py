"""Trace values PostgreSQL interprets as dates, without evaluating arbitrary SQL functions."""

import re
from dataclasses import replace

from sqlglot import exp
from sqlglot.optimizer.scope import Scope

from sidewit.validation.sql_analysis import FieldRef, SqlAnalysis
from sidewit.validation.sql_values import ValueAnalysis, ValueOrigin


def temporal_type(kind: exp.DataType) -> bool:
    if kind.is_type(*exp.DataType.TEMPORAL_TYPES):
        return True
    return kind.is_type(exp.DataType.Type.ARRAY) and any(
        isinstance(child, exp.DataType) and temporal_type(child) for child in kind.expressions
    )


def temporal_inputs(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.Cast) and temporal_type(node.to):
        return [node.this]
    if isinstance(node, exp.Date | exp.DateTrunc | exp.TimestampTrunc):
        return [node.this]
    if isinstance(node, exp.Anonymous) and node.name.lower() == "age":
        return list(node.expressions)
    return []


def special_time_literal(value: str) -> bool:
    return bool(re.match(r"^\s*(now|today|tomorrow|yesterday)(?:\s|$)", value, re.I))


class TemporalValues(ValueAnalysis):
    """Branch selection preserves possible date inputs; arbitrary text computations do not.

    UNKNOWN is rejected at temporal conversion sites. This prevents strings assembled by
    functions/concatenation from evading the reference-time policy. Database values and
    absolute caller values remain usable, including through CTEs and scalar subqueries.
    """

    def __init__(self, analysis: SqlAnalysis, temporal_fields: set[FieldRef]):
        super().__init__(analysis)
        self.temporal_fields = temporal_fields

    def source(self, source, name: str, seen: frozenset[tuple[int, int]]) -> list[ValueOrigin]:
        origins = super().source(source, name, seen)
        if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier):
            names = self.analysis.source_names(source)
            if names is not None and names.count(name) == 1:
                field = self.analysis.tables[source.name][names.index(name)]
                if (source.name, field) in self.temporal_fields:
                    return [replace(value, temporal=True) for value in origins]
        return origins

    def _resolve_values(
        self, scope: Scope, node: exp.Expression, seen: frozenset[tuple[int, int]]
    ) -> list[ValueOrigin]:
        if isinstance(node, exp.Cast):
            return [
                replace(value, temporal=temporal_type(node.to))
                for value in super()._resolve_values(scope, node, seen)
            ]
        if isinstance(node, exp.Coalesce):
            return [
                value
                for branch in [node.this, *node.expressions]
                for value in self.values(scope, branch, seen)
            ]
        if isinstance(node, exp.Case):
            branches = [branch.args["true"] for branch in node.args.get("ifs", [])]
            if node.args.get("default") is not None:
                branches.append(node.args["default"])
            return [v for branch in branches for v in self.values(scope, branch, seen)]
        if isinstance(node, exp.If):
            return [
                v
                for key in ("true", "false")
                if (branch := node.args.get(key)) is not None
                for v in self.values(scope, branch, seen)
            ]
        if isinstance(node, exp.Date | exp.DateTrunc | exp.TimestampTrunc):
            # Units/timezones describe the conversion, not the date input itself.
            return [replace(v, temporal=True) for v in self.values(scope, node.this, seen)]
        if isinstance(node, exp.AtTimeZone):
            return self.values(scope, node.this, seen)
        if isinstance(node, exp.Add | exp.Sub) and isinstance(node.expression, exp.Interval):
            return self.values(scope, node.this, seen)
        if isinstance(node, exp.Add) and isinstance(node.this, exp.Interval):
            return self.values(scope, node.expression, seen)
        return super()._resolve_values(scope, node, seen)
