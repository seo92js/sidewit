"""Deterministic SQL facts for golden expectations, not a proof of SQL equivalence."""

from collections.abc import Callable
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.schema import MappingSchema

from sidewit.domain.context import ContextDefinition
from sidewit.domain.dialect import Dialect, to_sqlglot
from sidewit.sql import COMPLEXITY_MESSAGE, parse_sql
from sidewit.validation.sql_validator import is_current_time

_OPS: dict[type[exp.Expression], str] = {
    exp.EQ: "=",
    exp.NEQ: "!=",
    # IS [NOT] DISTINCT FROM: the null-safe forms compare the same values.
    exp.NullSafeEQ: "=",
    exp.NullSafeNEQ: "!=",
    exp.GT: ">",
    exp.GTE: ">=",
    exp.LT: "<",
    exp.LTE: "<=",
}
_FLIPPED = {"=": "=", "!=": "!=", ">": "<", ">=": "<=", "<": ">", "<=": ">="}
_NEGATED = {"=": "!=", "!=": "=", ">": "<=", ">=": "<", "<": ">=", "<=": ">"}


@dataclass(frozen=True)
class Comparison:
    field: str
    op: str
    literals: tuple[str | bool, ...] = ()
    parameter: str | None = None


@dataclass
class SqlFacts:
    resources: set[str] = field(default_factory=set)
    fields: set[str] = field(default_factory=set)
    joins: set[frozenset[str]] = field(default_factory=set)
    aggregates: set[tuple[str, str, bool]] = field(default_factory=set)
    group_by: set[str] = field(default_factory=set)
    comparisons: list[Comparison] = field(default_factory=list)
    literals: list[str | bool] = field(default_factory=list)
    constructs: set[str] = field(default_factory=set)


class AnalysisError(Exception):
    pass


def analyze(statement: str, dialect: Dialect, context: ContextDefinition) -> SqlFacts:
    sqlglot_dialect = to_sqlglot(dialect)
    try:
        statements = [s for s in parse_sql(statement, sqlglot_dialect) if s]
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise AnalysisError("Exactly one SELECT query is required.")
        ast = statements[0]
        facts = SqlFacts()
        # Before qualify expands projection stars. COUNT(*) is not a projection star.
        if any(e.is_star for s in ast.find_all(exp.Select) for e in s.expressions):
            facts.constructs.add("select_star")
        if any(is_current_time(node) for node in ast.walk()):
            facts.constructs.add("relative_time_function")
        facts.literals = [
            value
            for node in ast.walk()
            if isinstance(node, exp.Literal | exp.Boolean | exp.Neg)
            and not isinstance(node.parent, exp.Neg)
            and (value := _literal(node)) is not None
        ]
        schema = {r.name: {f.name: "TEXT" for f in r.fields} for r in context.resources}
        ast = qualify(
            ast,
            schema=MappingSchema(schema, dialect=sqlglot_dialect, normalize=False),
            dialect=sqlglot_dialect,
            validate_qualify_columns=True,
            quote_identifiers=False,
            identify=False,
        )
        # Deliberately independent of the validator's lineage code: a validator bug must
        # not hide the same mistake from scoring.
        root = build_scope(ast)
        if root is None:
            raise AnalysisError("SQL has no query scope.")
        has_outer_join = any(
            join.side in {"LEFT", "RIGHT", "FULL"} for join in ast.find_all(exp.Join)
        )
        for scope in _reachable_scopes(root):
            for _, source in scope.selected_sources.values():
                if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier):
                    facts.resources.add(source.name.lower())
            select = scope.expression
            if not isinstance(select, exp.Select):
                continue
            for column in select.find_all(exp.Column):
                if column.find_ancestor(exp.Select) is select:
                    if (ref := _base_field(scope, column)) is not None:
                        facts.fields.add(ref)
            for projection in select.expressions:
                for aggregate in projection.find_all(exp.AggFunc):
                    if aggregate.find_ancestor(exp.Select) is not select:
                        continue
                    if aggregate.find_ancestor(exp.Window) is not None:
                        continue
                    target = aggregate.this
                    distinct = isinstance(target, exp.Distinct)
                    if distinct:
                        if len(target.expressions) != 1:
                            continue
                        target = target.expressions[0]
                    ref = _base_field(scope, target)
                    if isinstance(aggregate, exp.Count) and not distinct:
                        # COUNT(non-null field) and COUNT(non-null literal) count rows.
                        if isinstance(target, exp.Star | exp.Literal):
                            ref = "*"
                        elif ref and not has_outer_join:
                            table, name = ref.split(".")
                            resource = context.find_resource(table)
                            if resource and any(
                                f.name.lower() == name and (not f.nullable or f.primary_key)
                                for f in resource.fields
                            ):
                                ref = "*"
                    if ref:
                        facts.aggregates.add((aggregate.key, ref, distinct))
            if group := select.args.get("group"):
                for expression in group.expressions:
                    if ref := _base_field(scope, expression):
                        facts.group_by.add(ref)
            conditions = [select.args.get("where"), select.args.get("having")]
            conditions += [join.args.get("on") for join in select.args.get("joins") or []]
            for condition in filter(None, conditions):
                comparisons, joins = _collect(condition, lambda node, s=scope: _base_field(s, node))
                facts.comparisons.extend(sorted(comparisons, key=repr))
                facts.joins.update(joins)
        return facts
    except RecursionError:
        raise AnalysisError(COMPLEXITY_MESSAGE) from None
    except sqlglot.errors.SqlglotError as e:
        raise AnalysisError(f"SQL could not be analyzed: {e}") from e


def _reachable_scopes(root: Scope) -> list[Scope]:
    """Exclude unused CTE definitions; follow referenced CTEs, subqueries and UNION arms."""
    scopes: list[Scope] = []
    pending = [root]
    seen: set[int] = set()
    while pending:
        scope = pending.pop()
        if id(scope) in seen:
            continue
        seen.add(id(scope))
        scopes.append(scope)
        pending.extend(scope.subquery_scopes)
        pending.extend(scope.set_operation_scopes)
        pending.extend(s for _, s in scope.selected_sources.values() if isinstance(s, Scope))
    return scopes


def _base_field(
    scope: Scope, node: exp.Expression, seen: frozenset[tuple[int, str]] = frozenset()
) -> str | None:
    # Follow only direct column lineage and date/cast wrappers, not arbitrary arithmetic.
    while isinstance(node, exp.Alias | exp.Paren | exp.Cast | exp.Date | exp.TsOrDsToDate):
        node = node.this
    if not isinstance(node, exp.Column) or node.is_star:
        return None
    marker = (id(scope), node.sql())
    if marker in seen:
        return None
    seen = seen | {marker}
    current: Scope | None = scope
    while current is not None:
        source = next(
            (s for alias, (_, s) in current.selected_sources.items() if alias == node.table),
            None,
        )
        if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier):
            return f"{source.name}.{node.name}".lower()
        if isinstance(source, Scope):
            if not isinstance(source.expression, exp.Select):
                return None
            for index, output in enumerate(source.expression.expressions):
                name = source.outer_columns[index] if source.outer_columns else output.alias_or_name
                if name == node.name:
                    return _base_field(source, output, seen)
            return None
        if source is not None:
            return None
        current = current.parent
    return None


def _literal(node: exp.Expression) -> str | bool | None:
    while isinstance(node, exp.Cast | exp.Paren):
        node = node.this
    if isinstance(node, exp.Literal | exp.Boolean):
        return node.this
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and node.this.is_number:
        return f"-{node.this.this}"
    return None


def _collect(
    node: exp.Expression, base_field: Callable[[exp.Expression], str | None], negated: bool = False
) -> tuple[set[Comparison], set[frozenset[str]]]:
    """Only predicates required by a Boolean condition count. OR keeps common predicates;
    NOT is applied before matching. Nested SELECTs have their own scope and conditions."""
    if isinstance(node, exp.Where | exp.Having | exp.Paren):
        return _collect(node.this, base_field, negated)
    if isinstance(node, exp.Not):
        return _collect(node.this, base_field, not negated)
    if isinstance(node, exp.And | exp.Or):
        left, right = (
            _collect(node.this, base_field, negated),
            _collect(node.expression, base_field, negated),
        )
        conjunction = isinstance(node, exp.And) != negated
        return (
            (left[0] | right[0], left[1] | right[1])
            if conjunction
            else (left[0] & right[0], left[1] & right[1])
        )
    if isinstance(node, exp.In):
        ref = base_field(node.this)
        if ref is None or node.args.get("query") is not None:
            return set(), set()
        # Mixed/multi-value parameter lists are intentionally unsupported.
        literals = tuple(v for e in node.expressions if (v := _literal(e)) is not None)
        parameter = (
            node.expressions[0].name
            if len(node.expressions) == 1 and isinstance(node.expressions[0], exp.Placeholder)
            else None
        )
        return {Comparison(ref, "not in" if negated else "in", literals, parameter)}, set()
    if isinstance(node, exp.Between) and not negated:
        lower = exp.GTE(this=node.this.copy(), expression=node.args["low"].copy())
        upper = exp.LTE(this=node.this.copy(), expression=node.args["high"].copy())
        a, b = _collect(lower, base_field), _collect(upper, base_field)
        return a[0] | b[0], a[1] | b[1]
    if type(node) not in _OPS:
        return set(), set()
    left, right = node.this, node.expression
    left_ref, right_ref = base_field(left), base_field(right)
    op = _OPS[type(node)]
    if negated:
        op = _NEGATED[op]
    if left_ref and right_ref:
        joins = {frozenset((left_ref, right_ref))} if op == "=" else set()
        return set(), joins
    if left_ref:
        ref, other = left_ref, right
    elif right_ref:
        ref, other, op = right_ref, left, _FLIPPED[op]
    else:
        return set(), set()
    while isinstance(other, exp.Cast | exp.Paren):
        other = other.this
    if (literal := _literal(other)) is not None:
        return {Comparison(ref, op, (literal,))}, set()
    if isinstance(other, exp.Placeholder):
        return {Comparison(ref, op, parameter=other.name)}, set()
    return set(), set()
