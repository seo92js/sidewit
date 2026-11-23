"""The forms in which PostgreSQL compares two values, shared by every value policy.

Rows compare element by element: `(a, b) < (c, d)` is `a < c OR (a = c AND b < d)`, so
leading elements are also compared by equality.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from sqlglot import exp
from sqlglot.optimizer.scope import Scope

from sidewit.validation.sql_analysis import SqlAnalysis

_BINARY: dict[type[exp.Expression], str] = {
    exp.EQ: "=",
    exp.NullSafeEQ: "=",
    exp.NEQ: "!=",
    exp.NullSafeNEQ: "!=",
    exp.GT: ">",
    exp.GTE: ">=",
    exp.LT: "<",
    exp.LTE: "<=",
    exp.Like: "like",
    exp.ILike: "like",
}
FLIPPED = {"=": "=", "!=": "!=", ">": "<", ">=": "<=", "<": ">", "<=": ">=", "like": "like"}
# Under NOT; a negated LIKE still compares the same operands.
NEGATED = {"=": "!=", "!=": "=", ">": "<=", ">=": "<", "<": ">=", "<=": ">", "like": "like"}
EQUALITY = frozenset({"=", "!="})
_STRICT = {">": ">", ">=": ">", "<": "<", "<=": "<"}
NODES = (*_BINARY, exp.In, exp.Between, exp.Case, exp.Nullif)


@dataclass(frozen=True)
class Comparison:
    node: exp.Expression
    left: exp.Expression
    op: str
    right: exp.Expression | None  # None: no single value can be isolated
    column: int | None = None  # set when `right` is a subquery or VALUES list


def scope_comparisons(analysis: SqlAnalysis) -> list[tuple[Scope, Comparison]]:
    return [
        (scope, c)
        for scope in analysis.scopes
        for node in analysis.owned(scope, *NODES)
        for c in comparisons(node)
    ]


def comparisons(node: exp.Expression) -> list[Comparison]:
    return list(_comparisons(node))


def _comparisons(node: exp.Expression) -> Iterator[Comparison]:
    negated = _negated(node)
    if type(node) in _BINARY:
        op = _BINARY[type(node)]
        yield from _pairs(node, node.this, NEGATED[op] if negated else op, node.expression)
    elif isinstance(node, exp.In):
        if node.args.get("unnest") is not None:
            return  # `IN UNNEST(...)` is not PostgreSQL syntax
        op = "!=" if negated else "="
        query = node.args.get("query")
        for value in [query] if query is not None else node.expressions:
            yield from _pairs(node, node.this, op, value)
    elif isinstance(node, exp.Between):
        low_op, high_op = ("<", ">") if negated else (">=", "<=")
        bounds = [(low_op, node.args["low"]), (high_op, node.args["high"])]
        if node.args.get("symmetric"):
            bounds = [(op, bound) for _, bound in bounds for op in (low_op, high_op)]
        for op, bound in bounds:
            yield from _pairs(node, node.this, op, bound)
    elif isinstance(node, exp.Case):
        subject = node.this
        if subject is not None:  # simple CASE only
            for branch in node.args.get("ifs") or []:
                yield from _pairs(node, subject, "=", branch.this)
    elif isinstance(node, exp.Nullif):
        yield from _pairs(node, node.this, "=", node.expression)


def _negated(node: exp.Expression) -> bool:
    """Odd number of NOTs on the path through boolean connectives: by De Morgan,
    `NOT (a = 1 AND b = 2)` tests `a != 1` and `b != 2`."""
    negated = False
    parent = node.parent
    while isinstance(parent, exp.Paren | exp.And | exp.Or | exp.Not):
        negated ^= isinstance(parent, exp.Not)
        parent = parent.parent
    return negated


def _pairs(
    node: exp.Expression, left: exp.Expression, op: str, right: exp.Expression
) -> Iterator[Comparison]:
    left_row, right_row = _row(left), _row(right)
    if left_row is None and right_row is None:
        yield Comparison(node, left, op, right)
        return
    if left_row is None:
        yield from _pairs(node, right, FLIPPED[op], left)
        return
    query = _query(right)
    last = len(left_row) - 1
    for index, element in enumerate(left_row):
        if right_row is not None and len(right_row) == len(left_row):
            value, column = right_row[index], None
        elif query is not None:
            value, column = query, index
        else:
            value, column = None, None
        for element_op in _row_ops(op, index == last):
            yield Comparison(node, element, element_op, value, column)


def _row_ops(op: str, last: bool) -> tuple[str, ...]:
    if op in _STRICT and not last:
        return (_STRICT[op], "=")
    return (op,)


def _row(node: exp.Expression) -> list[exp.Expression] | None:
    node = node.unnest()
    return list(node.expressions) if isinstance(node, exp.Tuple) else None


def _query(node: exp.Expression) -> exp.Expression | None:
    while isinstance(node, exp.Paren | exp.Subquery):
        node = node.this
    return node if isinstance(node, exp.Query | exp.Values) else None
