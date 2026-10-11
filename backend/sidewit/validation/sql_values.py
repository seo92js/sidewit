"""Value origins for policy comparisons, using the structural analyzer's scopes.

A failed resolution is UNKNOWN, never a database value. Expressions retain their original
literal/parameter nodes; casts are carried separately so request-literal identity survives.
"""

from dataclasses import dataclass, replace
from typing import Literal

from sqlglot import exp
from sqlglot.optimizer.scope import Scope

from sidewit.domain.dialect import unbounded_text
from sidewit.validation.sql_analysis import SqlAnalysis, selected


@dataclass(frozen=True)
class ValueOrigin:
    kind: Literal["expression", "database", "unknown"]
    expression: exp.Expression | None = None
    casts: tuple[exp.DataType, ...] = ()  # inner to outer


UNKNOWN = ValueOrigin("unknown")
DATABASE = ValueOrigin("database")


class ValueAnalysis:
    def __init__(self, analysis: SqlAnalysis):
        self.analysis = analysis
        self.scopes = {id(scope.expression): scope for scope in analysis.scopes}

    def values(
        self, scope: Scope, node: exp.Expression, seen: frozenset[tuple[int, int]] = frozenset()
    ) -> list[ValueOrigin]:
        marker = (id(scope), id(node))
        if marker in seen:
            return [UNKNOWN]  # recursive output: no finite value proof
        seen = seen | {marker}
        if isinstance(node, exp.Alias | exp.Paren | exp.Collate):
            return self.values(scope, node.this, seen)
        if isinstance(node, exp.Cast):
            target = node.to
            if target.is_type(exp.DataType.Type.ARRAY):
                if len(target.expressions) != 1 or not unbounded_text(target.expressions[0]):
                    return [UNKNOWN]
                target = target.expressions[0]
            return [
                replace(v, casts=(*v.casts, target)) for v in self.values(scope, node.this, seen)
            ]
        if isinstance(node, exp.Subquery):
            return self.values(scope, node.this, seen)
        if isinstance(node, exp.Values):
            # SQLGlot creates a scope for FROM VALUES, but not scalar/IN VALUES.
            if any(len(row.expressions) != 1 for row in node.expressions):
                return [UNKNOWN]
            return [
                value
                for row in node.expressions
                for value in self.values(scope, row.expressions[0], seen)
            ]
        if isinstance(node, exp.Select | exp.SetOperation):
            query = self.scopes.get(id(node))
            outputs = self.analysis.outputs(query) if query is not None else None
            if outputs is None or len(outputs) != 1:
                return [UNKNOWN]
            return self.output(query, 0, seen)
        if isinstance(node, exp.Any | exp.All):
            return self.values(scope, node.this, seen)
        if isinstance(node, exp.Anonymous) and node.name.upper() in {"ANY", "ALL", "SOME"}:
            if len(node.expressions) == 1:
                return self.values(scope, node.expressions[0], seen)
            return [UNKNOWN]
        if isinstance(node, exp.Array):
            return [v for e in node.expressions for v in self.values(scope, e, seen)]
        if isinstance(node, exp.Placeholder | exp.Literal | exp.Boolean):
            return [ValueOrigin("expression", node)]
        if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
            return [ValueOrigin("expression", node)]
        if isinstance(node, exp.Null):
            return [DATABASE]  # NULL does not assert membership in a scalar domain
        if isinstance(node, exp.Column) and not node.is_star:
            if node.table:
                source = self.analysis.find_source(scope, node.table, node)
                return self.source(source, node.name, seen)
            for current, env in self.analysis.reachable(scope, node):
                matches, opaque = self.analysis.candidates(current, env, node.name)
                if opaque or len(matches) > 1:
                    return [UNKNOWN]
                if matches:
                    sources = selected(current)
                    return [
                        value
                        for alias in matches[0].aliases
                        for value in self.source(sources.get(alias), node.name, seen)
                    ]
            return [UNKNOWN]
        # A computed database column is not a caller value. Other computations require
        # semantics we do not implement; retain UNKNOWN even if a parameter is hidden
        # behind a derived column or a CTE.
        children = [v for e in node.iter_expressions() for v in self.values(scope, e, seen)]
        return [DATABASE if children and all(v.kind == "database" for v in children) else UNKNOWN]

    def source(self, source, name: str, seen: frozenset[tuple[int, int]]) -> list[ValueOrigin]:
        if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier):
            names = self.analysis.source_names(source)
            return [DATABASE if names is not None and names.count(name) == 1 else UNKNOWN]
        if not isinstance(source, Scope):
            return [UNKNOWN]
        outputs = self.analysis.outputs(source)
        positions = [i for i, column in enumerate(outputs or []) if column.name == name]
        return self.output(source, positions[0], seen) if len(positions) == 1 else [UNKNOWN]

    def output(
        self, scope: Scope, index: int, seen: frozenset[tuple[int, int]]
    ) -> list[ValueOrigin]:
        marker = (id(scope), index)
        if marker in seen:
            return [UNKNOWN]
        seen = seen | {marker}
        node = scope.expression
        if isinstance(node, exp.SetOperation):
            return [v for arm in scope.set_operation_scopes for v in self.output(arm, index, seen)]
        if isinstance(node, exp.Values):
            return [
                value
                for row in node.expressions
                for value in self.values(scope, row.expressions[index], seen)
            ]
        outputs = self.analysis.outputs(scope)
        if outputs is None or index >= len(outputs):
            return [UNKNOWN]
        column = outputs[index]
        if column.origin is not None:
            return [DATABASE]
        if column.expression is None:
            return [UNKNOWN]
        return self.values(column.scope, column.expression, seen)
