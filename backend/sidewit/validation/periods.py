"""Server-filled period bounds are half-open: `x >= :start` and `x < :end`, nothing else."""

from collections.abc import Sequence

from sqlglot import exp

from sidewit.domain.issue import Issue
from sidewit.validation.comparisons import FLIPPED, NODES, comparisons


def period_usage_issues(ast: exp.Expression, parameters: Sequence[tuple[str, str]]) -> list[Issue]:
    required = {start: ">=" for start, _ in parameters} | {end: "<" for _, end in parameters}
    correct: set[int] = set()
    for node in ast.find_all(*NODES):
        for c in comparisons(node):
            if c.right is None or c.column is not None:
                continue
            for bound, other, op in ((c.right, c.left, c.op), (c.left, c.right, FLIPPED[c.op])):
                placeholder = _placeholder(bound)
                if (
                    placeholder is not None
                    and required.get(placeholder.this) == op
                    and _placeholder(other) is None
                ):
                    correct.add(id(placeholder))
    issues = []
    for name in sorted(
        {
            p.this
            for p in ast.find_all(exp.Placeholder)
            if p.this in required and id(p) not in correct
        }
    ):
        issues.append(
            Issue(
                code="period_bound_misused",
                message=(
                    f"Period parameter ':{name}' is a half-open bound: use it only as "
                    f"`field {required[name]} :{name}`."
                ),
                ref=name,
            )
        )
    return issues


def _placeholder(node: exp.Expression) -> exp.Placeholder | None:
    # No casts: `:start::date` would truncate a server-computed timestamp.
    node = node.unnest()
    return node if isinstance(node, exp.Placeholder) else None
