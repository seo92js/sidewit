"""Server-filled period bounds are half-open: `x >= :start` and `x < :end`, nothing else."""

from collections import defaultdict
from collections.abc import Iterable, Sequence

from sqlglot import exp

from sidewit.domain.issue import Issue
from sidewit.validation.comparisons import FLIPPED, NODES, comparisons

# One entry per placeholder occurrence: (operator as a bound, compared expression), or None
# when that occurrence is not a bound of a comparison.
_Use = tuple[str, str] | None


def period_usage_issues(ast: exp.Expression, parameters: Sequence[tuple[str, str]]) -> list[Issue]:
    required = {start: ">=" for start, _ in parameters} | {end: "<" for _, end in parameters}
    uses = _bound_uses(ast)
    return [
        Issue(
            code="period_bound_misused",
            message=(
                f"Period parameter ':{name}' is a half-open bound: use it only as "
                f"`field {op} :{name}`."
            ),
            ref=name,
        )
        for name, op in sorted(required.items())
        if uses[name] and not _only(uses[name], op)
    ]


def period_inputs(ast: exp.Expression, names: Iterable[str]) -> list[tuple[str, str]]:
    """(start, end) pairs among `names` bounding the same expressions as a half-open range,
    so the server may fill them from a period."""
    uses = _bound_uses(ast)
    starts = {n: _compared(uses[n]) for n in names if _only(uses[n], ">=")}
    ends = {n: _compared(uses[n]) for n in names if _only(uses[n], "<")}
    pairs = []
    for start, compared in sorted(starts.items()):
        matches = [end for end, other in ends.items() if other == compared]
        if len(matches) == 1:
            pairs.append((start, matches[0]))
    return pairs


def _bound_uses(ast: exp.Expression) -> defaultdict[str, list[_Use]]:
    found: dict[int, tuple[str, str]] = {}
    for node in ast.find_all(*NODES):
        for c in comparisons(node):
            if c.right is None or c.column is not None:
                continue
            for bound, other, op in ((c.right, c.left, c.op), (c.left, c.right, FLIPPED[c.op])):
                placeholder = _placeholder(bound)
                if placeholder is not None and _placeholder(other) is None:
                    found[id(placeholder)] = (op, other.sql())
    uses: defaultdict[str, list[_Use]] = defaultdict(list)
    for placeholder in ast.find_all(exp.Placeholder):
        uses[placeholder.this].append(found.get(id(placeholder)))
    return uses


def _only(uses: list[_Use], op: str) -> bool:
    return bool(uses) and all(use is not None and use[0] == op for use in uses)


def _compared(uses: list[_Use]) -> frozenset[str]:
    return frozenset(use[1] for use in uses if use is not None)


def _placeholder(node: exp.Expression) -> exp.Placeholder | None:
    # No casts: `:start::date` would truncate a server-computed timestamp.
    node = node.unnest()
    return node if isinstance(node, exp.Placeholder) else None
